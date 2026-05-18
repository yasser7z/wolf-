# ===================================================================
#  ملف: bot.py
#  بوت ديسكورد - لعبة الذئب والقروي (Werewolf) متقدم
#  يدعم 4-15 لاعباً، أدوار متوازنة، نكات سعودية، أزرار دخول/خروج
#  يعمل على Render مع Python 3.13 + audioop-lts
# ===================================================================

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import asyncio
import random
import os
import sys
from typing import Dict, List, Optional, Set

# -------------------------------------------------------------------
# 0. التحقق من التوكن
# -------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("🚨 خطأ: لم يتم تعيين DISCORD_TOKEN في متغيرات البيئة.")
    sys.exit(1)

# -------------------------------------------------------------------
# 1. قاعدة البيانات (نقاط)
# -------------------------------------------------------------------
class Database:
    def __init__(self, db_path: str = "werewolf.db"):
        self.db_path = db_path

    async def init(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS points (
                user_id INTEGER PRIMARY KEY,
                points INTEGER DEFAULT 0
            )
        """)
        await self.conn.commit()

    async def add_points(self, user_id: int, points: int):
        await self.conn.execute(
            "INSERT INTO points (user_id, points) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET points = points + ?",
            (user_id, points, points)
        )
        await self.conn.commit()

    async def get_points(self, user_id: int) -> int:
        async with self.conn.execute("SELECT points FROM points WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def reset_all_points(self):
        await self.conn.execute("DELETE FROM points")
        await self.conn.commit()

# -------------------------------------------------------------------
# 2. تعريف الأدوار وخصائصها
# -------------------------------------------------------------------
class Role:
    def __init__(self, name_ar: str, team: str, night_action: Optional[str] = None, desc: str = "", once: bool = False):
        self.name_ar = name_ar
        self.team = team  # "wolf" or "village"
        self.night_action = night_action
        self.desc = desc
        self.once = once

# الأدوار الأساسية (سنكرر بعضها حسب الحاجة)
ROLES_DATA = {
    "الذيب": Role("الذيب 🐺", "wolf", "kill", "كل ليلة تتفق مع ربعك وتقتلون واحد.", False),
    "القروي": Role("القروي 🧑‍🌾", "village", None, "ما عندك صلاحيات، صوتك بس.", False),
    "المحقق": Role("المحقق 🔍", "village", "investigate", "مرة واحدة تعرف إذا اللاعب ذيب ولا قروي.", True),
    "الحارس": Role("الحارس 🛡️", "village", "protect", "مرة واحدة تحمي لاعب من الموت.", True),
    "الملك": Role("الملك 👑", "village", None, "مرة واحدة تعدم لاعب فوراً بدون تصويت.", True),
    "العمدة": Role("العمدة 🏛️", "village", None, "صوتك يحتسب صوتين ويكسر التعادل.", False),
    "الطبيب": Role("الطبيب ⚕️", "village", "heal", "كل ليلة تحاول تنقذ لاعب.", False),
    "المغرية": Role("المغرية 💃", "village", "block", "كل ليلة تزور لاعب: ذيب = موت سوا، قروي = حماية.", False),
    "أم زكي": Role("أم زكي 👵", "village", "revive", "مرة واحدة ترجع ميت إلى الحياة.", True)
}

# -------------------------------------------------------------------
# 3. آلة الحالة وجلسة اللعبة
# -------------------------------------------------------------------
class GamePhase:
    LOBBY = "lobby"
    NIGHT = "night"
    DAY_DISCUSSION = "day_discussion"
    DAY_VOTING = "day_voting"
    DAY_KING = "day_king"
    ENDED = "ended"

class GameSession:
    def __init__(self, guild_id: int, channel_id: int):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.phase = GamePhase.LOBBY
        self.players: List[int] = []          # جميع اللاعبين
        self.alive: List[int] = []            # الأحياء
        self.roles: Dict[int, Role] = {}
        self.wolf_team: List[int] = []        # أيدي الذئاب
        self.mayor_id: Optional[int] = None
        self.used_powers: Set[int] = set()    # للقدرات لمرة واحدة
        self.king_used: bool = False
        self.umm_zaki_used: bool = False

        # متغيرات الليل
        self.kill_target: Optional[int] = None
        self.heal_target: Optional[int] = None
        self.protect_target: Optional[int] = None
        self.block_target: Optional[int] = None
        self.investigate_target: Optional[int] = None
        self.revive_target: Optional[int] = None

        # متغيرات النهار
        self.votes: Dict[int, int] = {}
        self.has_voted: Set[int] = set()
        self.lynched: Optional[int] = None
        self.winner: Optional[str] = None

    def is_alive(self, uid: int) -> bool:
        return uid in self.alive

    def alive_wolves(self) -> List[int]:
        return [p for p in self.alive if self.roles[p].team == "wolf"]

    def alive_villagers(self) -> List[int]:
        return [p for p in self.alive if self.roles[p].team == "village"]

    def check_winner(self) -> bool:
        wolves = self.alive_wolves()
        villagers = self.alive_villagers()
        if len(wolves) == 0:
            self.winner = "village"
            return True
        if len(wolves) >= len(villagers):
            self.winner = "wolf"
            return True
        return False

# -------------------------------------------------------------------
# 4. النكات السعودية
# -------------------------------------------------------------------
JOKES = {
    "sleep": ["😴 نام نام، الحلم بيجيبك في الذيب.", "🌙 سكر عيونك، الدنيا غدر.", "💤 روق نام، الصبح مصايب."],
    "death": ["💀 مات... الله يرحمه.", "⚰️ اكلوه الذياب.", "🪦 RIP. باي."],
    "lynch": ["🔨 صلعناه بالغلط، آسفين.", "🗳️ راح ضحية ديمقراطيتنا.", "⚖️ أخطأنا وأصبنا."],
    "king_kill": ["👑 الملك أمر بإعدامك.", "🔪 مشوارك انتهى بأمر ملكي.", "⚔️ ما ينعزل الملك."],
    "wolf_win": ["🐺 فوز الذئاب! أكلوهم.", "🍖 عشاء لذيذ.", "🏆 الذياب أبطال."],
    "village_win": ["🏘️ فوز القرويين! نظفوا الديرة.", "🎉 كفو يا رجال.", "🌾 الحصاد وفير."],
    "no_kill": ["🛡️ ليلة هادئة، الحارس شغال.", "🍀 محد مات اليوم.", "💪 أبطال."],
    "investigate": ["🔎 اكتشفنا أنه {result}."]
}

def joke(cat: str, **kw) -> str:
    return random.choice(JOKES.get(cat, ["..."]*3)).format(**kw)

# -------------------------------------------------------------------
# 5. Cog اللعبة الرئيسي (بدون تذاكر)
# -------------------------------------------------------------------
class WerewolfCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db
        self.games: Dict[int, GameSession] = {}

    # ================== الأوامر المائلة ==================
    @app_commands.command(name="ذيب", description="فتح تسجيل اللعبة (4-15 لاعباً)")
    async def lobby_cmd(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        game = self.games.get(gid)
        if game and game.phase != GamePhase.LOBBY:
            await interaction.response.send_message("❌ فيه لعبة شغالة، انتظر.", ephemeral=True)
            return
        if not game:
            game = GameSession(gid, interaction.channel_id)
            self.games[gid] = game

        embed = discord.Embed(
            title="🐺 لعبة الذيب والقروي (النسخة المطورة) 🐺",
            description="سجل الآن بالضغط على الزر الأخضر. العدد: 4 إلى 15 لاعباً.\nكل 4 لاعبين يزيد ذيب واحد.",
            color=0x9b59b6
        )
        embed.add_field(name="👥 المسجلين", value="لا أحد", inline=False)
        view = LobbyView(self, gid)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="ابدأ_الذيب", description="بدء اللعبة بعد اكتمال التسجيل")
    async def start_cmd(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        game = self.games.get(gid)
        if not game or game.phase != GamePhase.LOBBY:
            await interaction.response.send_message("❌ ما فيه تسجيل مفتوح.", ephemeral=True)
            return
        num = len(game.players)
        if num < 4:
            await interaction.response.send_message(f"❌ العدد {num} قليل، يحتاج 4 لاعبين على الأقل.", ephemeral=True)
            return
        if num > 15:
            await interaction.response.send_message(f"❌ العدد {num} كثير، الحد 15 لاعباً.", ephemeral=True)
            return

        await self.assign_roles(game)
        game.phase = GamePhase.NIGHT

        # عرض أزرار الكشف عن الأدوار
        view = RevealRolesView(game.players, game.roles)
        await interaction.response.send_message("✅ تم توزيع الأدوار! اضغط الزر لمعرفة دورك (رسالة مخفية).", view=view)
        await asyncio.sleep(15)
        await self.start_night(interaction.channel, game)

    async def assign_roles(self, game: GameSession):
        """توزيع أدوار متوازن حسب عدد اللاعبين (ذيب واحد لكل 3-4 قرويين)"""
        num = len(game.players)
        # عدد الذئاب: 1 لكل 4 لاعبين (أقل عدد 1، أقصى 4 ذئاب عند 15 لاعباً)
        wolves_count = max(1, min(4, num // 4))
        role_pool = ["الذيب"] * wolves_count
        # قائمة الأدوار الخاصة (غير القروي وغير الذيب)
        specials = ["المحقق", "الحارس", "الملك", "العمدة", "الطبيب", "المغرية", "أم زكي"]
        random.shuffle(specials)
        # نضيف الأدوار الخاصة بقدر ما يسع العدد (بعد ترك مساحة للذئاب والقرويين)
        remaining = num - wolves_count
        specials_to_add = specials[:min(remaining, len(specials))]
        role_pool.extend(specials_to_add)
        # باقي العدد يملأ بالقروي
        while len(role_pool) < num:
            role_pool.append("القروي")
        random.shuffle(role_pool)

        # تعيين الأدوار وتخزينها
        for idx, pid in enumerate(game.players):
            role = ROLES_DATA[role_pool[idx]]
            game.roles[pid] = role
            game.alive.append(pid)
            if role.name_ar == "العمدة":
                game.mayor_id = pid
            if role.name_ar == "الذيب":
                game.wolf_team.append(pid)

    async def start_night(self, channel: discord.TextChannel, game: GameSession):
        game.phase = GamePhase.NIGHT
        game.kill_target = game.heal_target = game.protect_target = None
        game.block_target = game.investigate_target = game.revive_target = None

        await channel.send(joke("sleep"))
        await channel.send("🌙 **الليل يحل...** أصحاب القدرات الخاصة، لديكم 60 ثانية (سيتم إرسال الأزرار خاصاً).")

        for pid in game.alive:
            user = self.bot.get_user(pid)
            if not user: continue
            role = game.roles[pid]
            if role.night_action is None: continue
            if role.once and pid in game.used_powers:
                await user.send(f"⚠️ استخدمت قدرتك {role.name_ar} سابقاً.")
                continue
            view = NightActionView(self, game, pid)
            await user.send(f"🌙 **ليلتك كـ {role.name_ar}**\nاختر هدفك:", view=view)

        await asyncio.sleep(60)
        await self.resolve_night(channel, game)

    async def resolve_night(self, channel: discord.TextChannel, game: GameSession):
        # 1. المغرية (block)
        if game.block_target is not None:
            target = game.block_target
            sed = [p for p in game.alive if game.roles[p].night_action == "block"]
            if sed:
                sed_id = sed[0]
                if game.roles[target].team == "wolf":
                    if sed_id in game.alive: game.alive.remove(sed_id)
                    if target in game.alive: game.alive.remove(target)
                    await channel.send(f"💃 **المغرية** {self.bot.get_user(sed_id).mention} زارت ذيباً وماتوا معاً.")
                    if game.check_winner():
                        await self.end_game(channel, game)
                        return
                else:
                    game.protect_target = target
                    await channel.send(f"💃 **المغرية** حمت {self.bot.get_user(target).mention} الليلة.")

        # 2. أم زكي (إحياء)
        if game.revive_target is not None and game.revive_target not in game.alive:
            game.alive.append(game.revive_target)
            await channel.send(f"👵 **أم زكي** أعادت {self.bot.get_user(game.revive_target).mention} للحياة!")
            game.umm_zaki_used = True

        # 3. القتل
        kill = game.kill_target
        if kill is not None and kill in game.alive:
            if not (game.protect_target == kill or game.heal_target == kill):
                game.alive.remove(kill)
                await channel.send(f"💀 **الذيب قتل** {self.bot.get_user(kill).mention}\n{joke('death')}")
            else:
                await channel.send(f"🛡️ {self.bot.get_user(kill).mention} نجا بفضل الحماية/العلاج.")
        else:
            await channel.send(joke("no_kill"))

        # 4. المحقق
        if game.investigate_target is not None:
            det = [p for p in game.alive if game.roles[p].night_action == "investigate"]
            if det and game.investigate_target in game.alive:
                res = "ذيب 🐺" if game.roles[game.investigate_target].team == "wolf" else "قروي 🧑‍🌾"
                await self.bot.get_user(det[0]).send(f"🔍 {joke('investigate', result=res)}")
                game.used_powers.add(det[0])

        if game.check_winner():
            await self.end_game(channel, game)
            return

        game.phase = GamePhase.DAY_DISCUSSION
        await channel.send("☀️ **طلعت الشمس!** ابدوا النقاش. بعد دقيقتين يصير التصويت.")
        await asyncio.sleep(120)
        await self.start_voting(channel, game)

    async def start_voting(self, channel: discord.TextChannel, game: GameSession):
        game.phase = GamePhase.DAY_VOTING
        game.votes.clear()
        game.has_voted.clear()
        await channel.send("🗳️ **التصويت على الصلب!** لديكم 60 ثانية (سيتم إرسال الأزرار خاصاً).")

        for pid in game.alive:
            user = self.bot.get_user(pid)
            if user:
                view = VotingView(self, game, pid)
                await user.send("🗳️ اختر من تصلبه:", view=view)

        await asyncio.sleep(60)
        await self.resolve_voting(channel, game)

    async def resolve_voting(self, channel: discord.TextChannel, game: GameSession):
        if not game.votes:
            await channel.send("💤 لا أحد صوت، اليوم يمر بدون صلب.")
        else:
            maxv = max(game.votes.values())
            candidates = [pid for pid, cnt in game.votes.items() if cnt == maxv]
            if len(candidates) > 1 and game.mayor_id and game.mayor_id in game.alive:
                chosen = random.choice(candidates)
                await channel.send(f"🏛️ **العمدة** كسر التعادل واختار {self.bot.get_user(chosen).mention}.")
                game.lynched = chosen
            else:
                game.lynched = candidates[0]

            if game.lynched in game.alive:
                game.alive.remove(game.lynched)
                role = game.roles[game.lynched].name_ar
                await channel.send(f"⚰️ **تم صلب {self.bot.get_user(game.lynched).mention}!**\n{joke('lynch')}\nدوره: {role}")
            else:
                await channel.send("⚠️ خطأ في التصويت.")

        if game.check_winner():
            await self.end_game(channel, game)
            return

        # مرحلة الملك
        king = [p for p in game.alive if game.roles[p].name_ar == "الملك" and not game.king_used]
        if king:
            game.phase = GamePhase.DAY_KING
            await channel.send("👑 **للملك 30 ثانية ليعدم أحداً** (زر سيُرسل له خاصاً).")
            user = self.bot.get_user(king[0])
            if user:
                view = KingView(self, game, king[0])
                await user.send("👑 من تريد إعدامه فوراً؟", view=view)
                await asyncio.sleep(30)
            else:
                await self.start_night(channel, game)
        else:
            await self.start_night(channel, game)

    async def king_execute(self, game: GameSession, target: int, channel: discord.TextChannel):
        if target in game.alive:
            game.alive.remove(target)
            await channel.send(f"👑 **الملك أعدم {self.bot.get_user(target).mention}**\n{joke('king_kill')}\nدوره: {game.roles[target].name_ar}")
            game.king_used = True
            if game.check_winner():
                await self.end_game(channel, game)
                return
        await self.start_night(channel, game)

    async def end_game(self, channel: discord.TextChannel, game: GameSession):
        game.phase = GamePhase.ENDED
        if game.winner == "wolf":
            pts = 60
            winners = game.alive_wolves()
            msg = f"🐺 {joke('wolf_win')}\nكل ذيب حي يربح {pts} نقطة."
        else:
            pts = 45
            winners = game.alive_villagers()
            msg = f"🏘️ {joke('village_win')}\nكل قروي حي يربح {pts} نقطة."

        for pid in winners:
            await self.db.add_points(pid, pts)

        embed = discord.Embed(title="🏁 نهاية اللعبة", description=msg, color=0xf1c40f)
        for pid in game.players:
            user = self.bot.get_user(pid)
            if user:
                status = "❤️ حي" if pid in game.alive else "💀 ميت"
                embed.add_field(name=user.display_name, value=f"{game.roles[pid].name_ar} ({status})", inline=True)
        await channel.send(embed=embed)
        self.games.pop(game.guild_id, None)

    @app_commands.command(name="نقاطي", description="عرض نقاطك")
    async def points_cmd(self, interaction: discord.Interaction):
        pts = await self.db.get_points(interaction.user.id)
        await interaction.response.send_message(f"📊 نقاطك: **{pts}**", ephemeral=True)

    @app_commands.command(name="تصفير_الذيب", description="تصفير النقاط (للمشرفين)")
    @commands.has_permissions(administrator=True)
    async def reset_cmd(self, interaction: discord.Interaction):
        await self.db.reset_all_points()
        await interaction.response.send_message("✅ تم تصفير جميع النقاط.", ephemeral=True)


# -------------------------------------------------------------------
# 6. الواجهات التفاعلية (أزرار الدخول/الخروج، التصويت، إلخ)
# -------------------------------------------------------------------
class LobbyView(discord.ui.View):
    def __init__(self, cog: WerewolfCog, guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="دخول 🐺", style=discord.ButtonStyle.green)
    async def join(self, interaction: discord.Interaction, btn: discord.ui.Button):
        game = self.cog.games.get(self.guild_id)
        if not game or game.phase != GamePhase.LOBBY:
            await interaction.response.send_message("التسجيل مقفل.", ephemeral=True)
            return
        if interaction.user.id in game.players:
            await interaction.response.send_message("أنت مسجل مسبقاً.", ephemeral=True)
            return
        if len(game.players) >= 15:
            await interaction.response.send_message("اكتمل العدد (15)، لا يمكن الدخول.", ephemeral=True)
            return
        game.players.append(interaction.user.id)
        embed = interaction.message.embeds[0]
        names = ", ".join([f"<@{p}>" for p in game.players]) if game.players else "لا أحد"
        embed.set_field_at(0, name="👥 المسجلين", value=names, inline=False)
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message(f"✅ تم دخولك! عدد اللاعبين: {len(game.players)}", ephemeral=True)

    @discord.ui.button(label="خروج 🚪", style=discord.ButtonStyle.red)
    async def leave(self, interaction: discord.Interaction, btn: discord.ui.Button):
        game = self.cog.games.get(self.guild_id)
        if not game or game.phase != GamePhase.LOBBY:
            await interaction.response.send_message("ما تقدر تخرج الآن.", ephemeral=True)
            return
        if interaction.user.id not in game.players:
            await interaction.response.send_message("ما أنت مسجل.", ephemeral=True)
            return
        game.players.remove(interaction.user.id)
        embed = interaction.message.embeds[0]
        names = ", ".join([f"<@{p}>" for p in game.players]) if game.players else "لا أحد"
        embed.set_field_at(0, name="👥 المسجلين", value=names, inline=False)
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message("🚪 خرجت من التسجيل.", ephemeral=True)

class RevealRolesView(discord.ui.View):
    def __init__(self, players: List[int], roles: Dict[int, Role]):
        super().__init__(timeout=120)
        self.players = players
        self.roles = roles

    @discord.ui.button(label="اعرف دورك 🔮", style=discord.ButtonStyle.primary)
    async def reveal(self, interaction: discord.Interaction, btn: discord.ui.Button):
        if interaction.user.id not in self.players:
            await interaction.response.send_message("لست مشاركاً.", ephemeral=True)
            return
        role = self.roles[interaction.user.id]
        embed = discord.Embed(title="دورك السري", description=f"**{role.name_ar}**\n{role.desc}", color=0x9b59b6)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        btn.disabled = True
        await interaction.message.edit(view=self)

class NightActionView(discord.ui.View):
    def __init__(self, cog: WerewolfCog, game: GameSession, pid: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.game = game
        self.pid = pid
        self.action = game.roles[pid].night_action
        targets = [p for p in game.alive if p != pid]
        if not targets:
            return
        select = discord.ui.Select(placeholder="اختر هدفاً")
        for t in targets:
            user = cog.bot.get_user(t)
            if user:
                select.add_option(label=user.display_name, value=str(t))
        select.callback = self.callback
        self.add_item(select)

    async def callback(self, interaction: discord.Interaction):
        target = int(interaction.data["values"][0])
        if self.action == "kill":
            self.game.kill_target = target
        elif self.action == "heal":
            self.game.heal_target = target
        elif self.action == "protect":
            self.game.protect_target = target
        elif self.action == "block":
            self.game.block_target = target
        elif self.action == "investigate":
            if self.pid in self.game.used_powers:
                await interaction.response.send_message("استخدمتها مرة واحدة!", ephemeral=True)
                return
            self.game.investigate_target = target
            self.game.used_powers.add(self.pid)
        elif self.action == "revive":
            if self.game.umm_zaki_used:
                await interaction.response.send_message("أم زكي استخدمت الإحياء مسبقاً!", ephemeral=True)
                return
            if target not in self.game.players or target in self.game.alive:
                await interaction.response.send_message("الهدف حي أو غير موجود!", ephemeral=True)
                return
            self.game.revive_target = target
            self.game.umm_zaki_used = True
        await interaction.response.send_message("✅ تم استلام اختيارك.", ephemeral=True)
        self.stop()

class VotingView(discord.ui.View):
    def __init__(self, cog: WerewolfCog, game: GameSession, voter: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.game = game
        self.voter = voter
        targets = [p for p in game.alive if p != voter]
        if not targets:
            return
        select = discord.ui.Select(placeholder="اختر من تصلبه")
        for t in targets:
            user = cog.bot.get_user(t)
            if user:
                select.add_option(label=user.display_name, value=str(t))
        select.callback = self.vote
        self.add_item(select)

    async def vote(self, interaction: discord.Interaction):
        target = int(interaction.data["values"][0])
        if self.voter in self.game.has_voted:
            await interaction.response.send_message("صوت مسبقاً!", ephemeral=True)
            return
        weight = 2 if self.game.roles[self.voter].name_ar == "العمدة" else 1
        self.game.votes[target] = self.game.votes.get(target, 0) + weight
        self.game.has_voted.add(self.voter)
        await interaction.response.send_message(f"✅ صوتك مسجل (وزنه {weight})", ephemeral=True)
        self.stop()

class KingView(discord.ui.View):
    def __init__(self, cog: WerewolfCog, game: GameSession, king_id: int):
        super().__init__(timeout=30)
        self.cog = cog
        self.game = game
        self.king = king_id
        targets = [p for p in game.alive if p != king_id]
        if not targets:
            return
        select = discord.ui.Select(placeholder="اختر من تعدمه")
        for t in targets:
            user = cog.bot.get_user(t)
            if user:
                select.add_option(label=user.display_name, value=str(t))
        select.callback = self.execute
        self.add_item(select)

    async def execute(self, interaction: discord.Interaction):
        target = int(interaction.data["values"][0])
        ch = self.cog.bot.get_channel(self.game.channel_id)
        await self.cog.king_execute(self.game, target, ch)
        await interaction.response.send_message("تم تنفيذ الأمر الملكي.", ephemeral=True)
        self.stop()

# -------------------------------------------------------------------
# 7. البوت الرئيسي
# -------------------------------------------------------------------
class PremiumBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database()

    async def setup_hook(self):
        await self.db.init()
        await self.add_cog(WerewolfCog(self, self.db))
        await self.tree.sync()
        print("✅ تم تحميل البوت بنجاح!")

    async def on_ready(self):
        print(f"🤖 {self.user} شغال بكامل قوته!")
        print(f"✅ متصل على {len(self.guilds)} سيرفر.")

# -------------------------------------------------------------------
# 8. التشغيل
# -------------------------------------------------------------------
if __name__ == "__main__":
    bot = PremiumBot()
    bot.run(TOKEN)