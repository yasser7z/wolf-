# ===================================================================
#  ملف: bot.py
#  بوت ديسكورد - لعبة الذئب والقروي (Werewolf) + نظام تذاكر + نكات
#  يدعم 9 أدوار، نقاط، WAL، رسائل Ephemeral، نكات سعودية مضحكة
#  متوافق مع Python 3.13 / 3.12
# ===================================================================

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import asyncio
import random
import os
import sys
from typing import Dict, List, Optional, Set, Union
from datetime import datetime

# -------------------------------------------------------------------
# 0. التحقق من التوكن فوراً قبل أي شيء
# -------------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("🚨 خطأ فادح: لم يتم تعيين DISCORD_TOKEN في متغيرات البيئة.")
    print("الرجاء إضافة التوكن في إعدادات Render ثم إعادة النشر.")
    sys.exit(1)
if len(TOKEN) < 50:
    print("🚨 التوكن يبدو قصيراً جداً. تأكد من نسخه كاملاً من Discord Developer Portal.")
    sys.exit(1)

# -------------------------------------------------------------------
# 1. قاعدة البيانات (SQLite + WAL)
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

    async def close(self):
        await self.conn.close()

# -------------------------------------------------------------------
# 2. الأدوار والخصائص
# -------------------------------------------------------------------
class Role:
    def __init__(self, name_ar: str, team: str, night_action: Optional[str] = None, description: str = "", can_use_once: bool = False):
        self.name_ar = name_ar
        self.team = team
        self.night_action = night_action
        self.description = description
        self.can_use_once = can_use_once

ROLES_DATA = {
    "الذيب": Role("الذيب 🐺", "wolf", "kill", "كل ليلة تتفق مع ربعك وتاكلون واحد.", False),
    "القروي": Role("القروي 🧑‍🌾", "village", None, "ما عندك صلاحيات، بس صوتك يقرر المصير.", False),
    "المحقق": Role("المحقق 🔍", "village", "investigate", "مرة واحدة تعرف إذا اللاعب ذيب ولا قروي.", True),
    "الحارس": Role("الحارس 🛡️", "village", "protect", "مرة واحدة تحمي لاعب من الموت.", True),
    "الملك": Role("الملك 👑", "village", None, "مرة واحدة تقدر تعدم أي لاعب بدون تصويت.", True),
    "العمدة": Role("العمدة 🏛️", "village", None, "صوتك في التصويت يحتسب صوتين.", False),
    "الطبيب": Role("الطبيب ⚕️", "village", "heal", "كل ليلة تحاول تنقذ لاعب من الموت.", False),
    "المغرية": Role("المغرية 💃", "village", "block", "كل ليلة تزور لاعب: إن كان ذيب يموتون سوا، وإن كان قروي يتحصن.", False),
    "أم زكي": Role("أم زكي 👵", "village", "revive", "مرة واحدة ترجع ميت إلى الحياة.", True)
}

# -------------------------------------------------------------------
# 3. آلة الحالة (State Machine)
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
        self.players: List[int] = []
        self.alive: List[int] = []
        self.roles: Dict[int, Role] = {}
        self.wolf_players: List[int] = []
        self.mayor_id: Optional[int] = None
        self.used_powers: Set[int] = set()
        self.king_used: bool = False
        self.umm_zaki_used: bool = False
        # متغيرات الليل
        self.night_actions: Dict[str, Optional[int]] = {}
        self.kill_target: Optional[int] = None
        self.heal_target: Optional[int] = None
        self.protect_target: Optional[int] = None
        self.block_target: Optional[int] = None
        self.investigate_target: Optional[int] = None
        self.revive_target: Optional[int] = None
        # متغيرات النهار
        self.votes: Dict[int, int] = {}
        self.has_voted: Set[int] = set()
        self.lynched_player: Optional[int] = None
        self.winner: Optional[str] = None

    def is_alive(self, uid: int) -> bool:
        return uid in self.alive

    def get_alive_wolves(self) -> List[int]:
        return [p for p in self.alive if self.roles[p].team == "wolf"]

    def get_alive_villagers(self) -> List[int]:
        return [p for p in self.alive if self.roles[p].team == "village"]

    def check_game_over(self) -> bool:
        wolves = self.get_alive_wolves()
        villagers = self.get_alive_villagers()
        if len(wolves) == 0:
            self.winner = "village"
            return True
        if len(wolves) >= len(villagers):
            self.winner = "wolf"
            return True
        return False

# -------------------------------------------------------------------
# 4. النكات السعودية الجاهزة (للموت، النوم، التصويت، الفوز)
# -------------------------------------------------------------------
JOKES = {
    "sleep": [
        "😴 نام نام يا حلو، الحلم بيجيبك في الذيب.",
        "🛌 غطي نفسك كويس، الذيب يصحى الحين.",
        "🌙 الليل دخل والكل يغمض عيونه... إلا الذياب، عيونهم حمرا.",
        "💤 ناموا القرويين وفتحت الذياب الجوالات تتفق."
    ],
    "death": [
        "💀 مات... الله يرحمه كان ضحية بريئة (مع إن ريحته مشبوهة).",
        "⚰️ انقضوا عليه الذياب وماخلوا غير عظمة.",
        "😵 مات بالهبل، كان المفروض يسمع كلام أمه وما ينضم للعبة.",
        "🪦 RIP. بكره الصبح أهله بيدورونه ما يلقونه."
    ],
    "lynch": [
        "🔨 صلعناه بالعافية... طلع قروي مسكين، يلا مع السلامة.",
        "🗳️ جماهير القرية صوّتت لك بالإجماع... جرب حظك بالمكان الثاني.",
        "⚖️ القصاص العادل: أكلوا كم واحد بريء بس خلاص.",
        "🤣 شفنا انك غريب، طلعنا غلطانين... ضحكنا عليك."
    ],
    "king_kill": [
        "👑 الملك أمر بقطع رقبتك. لا تعترض، هو ملك.",
        "🔪 تم الإعدام الملكي الفوري... خط أحمر يا غالي.",
        "⚔️ الملك ما يهز رأسه إلا ويسقط رأسك."
    ],
    "wolf_win": [
        "🐺 الذياب أكلوا الجميع وهجوا الجبل... انتصروا بجدارة.",
        "🍖 صار العيد عند الذياب، شبعة لحم قروي.",
        "🏆 فوز الذئاب: المدرسة اللي دربهم كان قاسي لكن طلعوا عباقرة."
    ],
    "village_win": [
        "🏘️ القرويون اكتشفوا الخونة وعلقوهم... عاش العدل.",
        "🎉 كفو يا شباب، نظفتوا الديرة من العفن.",
        "🌾 القمح حصاده هذا العام وفير بسبب طرد الذياب."
    ],
    "no_kill": [
        "🛡️ الليلة ما مات أحد... الطبيب شغال صح أو الحارس مخبي.",
        "🍀 حظ القرويين اليوم حلو، كلهم ناموا وسالموا.",
        "💪 رجال القرية أقوياء، الذياب فشلت هجومها."
    ],
    "investigate": [
        "🔎 فتشنا عنه طلع **{result}**... يا سلام على المباحث.",
        "🕵️ التحقيق كشف: هذا **{result}** 100%."
    ]
}

def get_joke(category: str, **kwargs) -> str:
    jokes_list = JOKES.get(category, ["..."]*5)
    joke = random.choice(jokes_list)
    return joke.format(**kwargs)

# -------------------------------------------------------------------
# 5. Cog اللعبة الرئيسي
# -------------------------------------------------------------------
class WerewolfCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db
        self.games: Dict[int, GameSession] = {}

    # ============== الأوامر الرئيسية ==============
    @app_commands.command(name="ذيب", description="افتح تسجيل لعبة القرويين والذئاب")
    async def lobby_cmd(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        game = self.games.get(gid)
        if game and game.phase != GamePhase.LOBBY:
            await interaction.response.send_message("❌ فيه لعبة شغالة حالياً، انتظر.", ephemeral=True)
            return
        if not game:
            game = GameSession(gid, interaction.channel_id)
            self.games[gid] = game

        embed = discord.Embed(
            title="🐺 فزعتكم يا رجال! لعبة الذيب والقروي 🐺",
            description="سجل الآن واشترك في الإثارة.\nالعدد: 4-9 لاعبين.",
            color=0x9b59b6
        )
        embed.add_field(name="👥 المسجلين", value="لا أحد بعد", inline=False)
        embed.set_footer(text="بعد التسجيل، اكتب /ابدأ_الذيب")
        view = RegistrationView(self, gid)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="ابدأ_الذيب", description="ابدأ اللعبة بعد اكتمال التسجيل")
    async def start_cmd(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        game = self.games.get(gid)
        if not game or game.phase != GamePhase.LOBBY:
            await interaction.response.send_message("❌ ما فيه تسجيل مفتوح.", ephemeral=True)
            return
        if len(game.players) < 4:
            await interaction.response.send_message(f"❌ العدد {len(game.players)} قليل، يحتاج 4 على الأقل.", ephemeral=True)
            return
        if len(game.players) > 9:
            await interaction.response.send_message(f"❌ العدد {len(game.players)} كثير، الحد 9.", ephemeral=True)
            return

        await self.assign_roles(game)
        game.phase = GamePhase.NIGHT

        # إرسال بطاقات الأدوار (رسائل مخفية)
        view = RevealRolesView(game.players, game.roles)
        await interaction.response.send_message("✅ تم توزيع الأدوار! اضغط الزر لمعرفة دورك.", view=view)

        await asyncio.sleep(15)
        await self.start_night_phase(interaction.channel, game)

    async def assign_roles(self, game: GameSession):
        """توزيع متوازن للأدوار بناءً على عدد اللاعبين"""
        num = len(game.players)
        role_names = []
        # قاعدة: ذيب واحد + أدوار خاصة بقدر العدد ثم قرويين
        role_names.append("الذيب")
        specials = ["المحقق", "الطبيب", "الحارس", "المغرية", "الملك", "العمدة", "أم زكي"]
        random.shuffle(specials)
        # إضافة الأدوار الخاصة حسب المساحة (num-2 لأن ذيب + قروي أساسي واحد على الأقل)
        for i in range(min(num-2, len(specials))):
            role_names.append(specials[i])
        # ملء الباقي بالقروي
        while len(role_names) < num:
            role_names.append("القروي")
        random.shuffle(role_names)

        for idx, pid in enumerate(game.players):
            role = ROLES_DATA[role_names[idx]]
            game.roles[pid] = role
            game.alive.append(pid)
            if role.name_ar == "العمدة":
                game.mayor_id = pid
            if role.name_ar == "الذيب":
                game.wolf_players.append(pid)

    async def start_night_phase(self, channel: discord.TextChannel, game: GameSession):
        game.phase = GamePhase.NIGHT
        # reset night vars
        game.kill_target = game.heal_target = game.protect_target = game.block_target = None
        game.investigate_target = game.revive_target = None

        await channel.send(get_joke("sleep"))
        await channel.send("🌙 **الليل يحل...** أصحاب القدرات الخاصة، لديكم 60 ثانية لاستخدام قدراتكم (سيتم إرسال الأزرار خاصاً).")

        for pid in game.alive:
            user = self.bot.get_user(pid)
            if not user: continue
            role = game.roles[pid]
            if role.night_action is None: continue
            if role.can_use_once and pid in game.used_powers:
                await user.send(f"⚠️ لقد استخدمت قدرتك {role.name_ar} سابقاً، لا يمكنك استخدامها مجدداً.")
                continue
            view = NightActionView(self, game, pid)
            await user.send(f"🌙 **ليلتك كـ {role.name_ar}**\nاختر هدفك:", view=view)

        await asyncio.sleep(60)
        await self.resolve_night_actions(channel, game)

    async def resolve_night_actions(self, channel: discord.TextChannel, game: GameSession):
        # 1. المغرية (block)
        if game.block_target is not None:
            target = game.block_target
            seductress = [p for p in game.alive if game.roles[p].night_action == "block"]
            if seductress:
                sed_id = seductress[0]
                if game.roles[target].team == "wolf":
                    # يموتون سوا
                    if sed_id in game.alive: game.alive.remove(sed_id)
                    if target in game.alive: game.alive.remove(target)
                    await channel.send(f"💃 **المغرية** {self.bot.get_user(sed_id).mention} زارت {self.bot.get_user(target).mention} اللي طلع ذيب! وماتوا الاثنين.")
                    if game.check_game_over():
                        await self.end_game(channel, game)
                        return
                else:
                    # حماية للقروي
                    game.protect_target = target
                    await channel.send(f"💃 **المغرية** حمت {self.bot.get_user(target).mention} من الذياب الليلة.")

        # 2. أم زكي (إحياء)
        if game.revive_target is not None and game.revive_target not in game.alive:
            game.alive.append(game.revive_target)
            await channel.send(f"👵 **أم زكي** أعادت {self.bot.get_user(game.revive_target).mention} إلى الحياة! يالله معجزة.")
            game.umm_zaki_used = True

        # 3. القتل
        kill = game.kill_target
        if kill is not None and kill in game.alive:
            protected = (game.protect_target == kill)
            healed = (game.heal_target == kill)
            if not (protected or healed):
                game.alive.remove(kill)
                await channel.send(f"💀 **الذيب قتل** {self.bot.get_user(kill).mention}\n{get_joke('death')}")
            else:
                await channel.send(f"🛡️ **تم إنقاذ** {self.bot.get_user(kill).mention} بواسطة الحماية أو العلاج.")
        else:
            await channel.send(get_joke("no_kill"))

        # 4. تحقيق المحقق
        if game.investigate_target is not None:
            det = [p for p in game.alive if game.roles[p].night_action == "investigate"]
            if det and game.investigate_target in game.alive:
                result = "ذيب 🐺" if game.roles[game.investigate_target].team == "wolf" else "قروي 🧑‍🌾"
                joke = get_joke("investigate", result=result)
                await self.bot.get_user(det[0]).send(f"🔍 {joke}")
                game.used_powers.add(det[0])

        if game.check_game_over():
            await self.end_game(channel, game)
            return

        game.phase = GamePhase.DAY_DISCUSSION
        await channel.send("☀️ **طلع الصبح!** ابدوا نقاشكم واتهموا الخونة. بعد دقيقتين يصير التصويت.")
        await asyncio.sleep(120)
        await self.start_day_voting(channel, game)

    async def start_day_voting(self, channel: discord.TextChannel, game: GameSession):
        game.phase = GamePhase.DAY_VOTING
        game.votes.clear()
        game.has_voted.clear()
        await channel.send("🗳️ **التصويت على الصلب!** كل لاعب يختار من يصلب (لديك 60 ثانية).")

        for pid in game.alive:
            user = self.bot.get_user(pid)
            if user:
                view = VotingView(self, game, pid)
                await user.send("🗳️ اختر من تصلبه:", view=view)

        await asyncio.sleep(60)
        await self.resolve_day_voting(channel, game)

    async def resolve_day_voting(self, channel: discord.TextChannel, game: GameSession):
        if not game.votes:
            await channel.send("💤 لا أحد صوت، اليوم يمر بدون صلب.")
        else:
            max_votes = max(game.votes.values())
            candidates = [pid for pid, cnt in game.votes.items() if cnt == max_votes]
            if len(candidates) > 1 and game.mayor_id and game.mayor_id in game.alive:
                chosen = random.choice(candidates)
                await channel.send(f"🏛️ **العمدة** كسر التعادل ووقع الاختيار على {self.bot.get_user(chosen).mention}.")
                game.lynched_player = chosen
            else:
                game.lynched_player = candidates[0]

            if game.lynched_player in game.alive:
                game.alive.remove(game.lynched_player)
                role_name = game.roles[game.lynched_player].name_ar
                await channel.send(f"⚰️ **تم صلب {self.bot.get_user(game.lynched_player).mention}!**\n{get_joke('lynch')}\nكان دوره {role_name}.")
            else:
                await channel.send("⚠️ حدث خطأ في التصويت.")

        if game.check_game_over():
            await self.end_game(channel, game)
            return

        # مرحلة الملك (إن لم يستخدم)
        king = [p for p in game.alive if game.roles[p].name_ar == "الملك" and not game.king_used]
        if king:
            game.phase = GamePhase.DAY_KING
            await channel.send("👑 **للملك الحق في إعدام أحد المشتبه بهم فوراً (مرة واحدة).** لديه 30 ثانية.")
            user = self.bot.get_user(king[0])
            if user:
                view = KingExecutionView(self, game, king[0])
                await user.send("👑 من تريد إعدامه بأمر ملكي؟", view=view)
                await asyncio.sleep(30)
            else:
                await self.start_night_phase(channel, game)
        else:
            await self.start_night_phase(channel, game)

    async def execute_king_execution(self, game: GameSession, target: int, channel: discord.TextChannel):
        if target in game.alive:
            game.alive.remove(target)
            await channel.send(f"👑 **بأمر الملك {self.bot.get_user(target).mention} أعدم فوراً!**\n{get_joke('king_kill')}\nدوره: {game.roles[target].name_ar}")
            game.king_used = True
            if game.check_game_over():
                await self.end_game(channel, game)
                return
        await self.start_night_phase(channel, game)

    async def end_game(self, channel: discord.TextChannel, game: GameSession):
        game.phase = GamePhase.ENDED
        if game.winner == "wolf":
            points = 60
            winners = game.get_alive_wolves()
            msg = f"🐺 {get_joke('wolf_win')}\nكل ذيب حي يربح {points} نقطة."
        else:
            points = 45
            winners = game.get_alive_villagers()
            msg = f"🏘️ {get_joke('village_win')}\nكل قروي حي يربح {points} نقطة."

        for pid in winners:
            await self.db.add_points(pid, points)

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
        await interaction.response.send_message(f"📊 {interaction.user.mention} نقاطك: **{pts}** نقطة.", ephemeral=True)

    @app_commands.command(name="تصفير_الذيب", description="تصفير النقاط (للمشرفين)")
    @commands.has_permissions(administrator=True)
    async def reset_cmd(self, interaction: discord.Interaction):
        await self.db.reset_all_points()
        await interaction.response.send_message("✅ تم تصفير جميع النقاط.", ephemeral=True)


# -------------------------------------------------------------------
# 6. واجهات المستخدم التفاعلية (Views)
# -------------------------------------------------------------------
class RegistrationView(discord.ui.View):
    def __init__(self, cog: WerewolfCog, guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="انضمام 🐺", style=discord.ButtonStyle.green)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.cog.games.get(self.guild_id)
        if not game or game.phase != GamePhase.LOBBY:
            await interaction.response.send_message("التسجيل مقفل.", ephemeral=True)
            return
        if interaction.user.id in game.players:
            await interaction.response.send_message("أنت مسجل مسبقاً.", ephemeral=True)
            return
        game.players.append(interaction.user.id)
        embed = interaction.message.embeds[0]
        names = ", ".join([f"<@{p}>" for p in game.players]) if game.players else "لا أحد"
        embed.set_field_at(0, name="👥 المسجلين", value=names, inline=False)
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message("✅ تم انضمامك! الآن انتظر أمر /ابدأ_الذيب", ephemeral=True)

    @discord.ui.button(label="انسحاب 🚪", style=discord.ButtonStyle.red)
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.cog.games.get(self.guild_id)
        if not game or game.phase != GamePhase.LOBBY:
            await interaction.response.send_message("ما تقدر تنسحب الآن.", ephemeral=True)
            return
        if interaction.user.id not in game.players:
            await interaction.response.send_message("ما أنت مسجل.", ephemeral=True)
            return
        game.players.remove(interaction.user.id)
        embed = interaction.message.embeds[0]
        names = ", ".join([f"<@{p}>" for p in game.players]) if game.players else "لا أحد"
        embed.set_field_at(0, name="👥 المسجلين", value=names, inline=False)
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message("🚪 غادرت التسجيل.", ephemeral=True)

class RevealRolesView(discord.ui.View):
    def __init__(self, players: List[int], roles: Dict[int, Role]):
        super().__init__(timeout=120)
        self.players = players
        self.roles = roles

    @discord.ui.button(label="اعرف دورك 🔮", style=discord.ButtonStyle.primary)
    async def reveal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.players:
            await interaction.response.send_message("أنت لست مشاركاً.", ephemeral=True)
            return
        role = self.roles[interaction.user.id]
        embed = discord.Embed(title="🔮 دورك السري", description=f"أنت **{role.name_ar}**\n{role.description}", color=0x9b59b6)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        button.disabled = True
        await interaction.message.edit(view=self)

class NightActionView(discord.ui.View):
    def __init__(self, cog: WerewolfCog, game: GameSession, player_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.game = game
        self.player_id = player_id
        self.action = game.roles[player_id].night_action
        targets = [p for p in game.alive if p != player_id]
        if not targets:
            return
        select = discord.ui.Select(placeholder=f"اختر هدفاً")
        for t in targets:
            user = cog.bot.get_user(t)
            if user:
                select.add_option(label=user.display_name, value=str(t))
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
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
            if self.player_id in self.game.used_powers:
                await interaction.response.send_message("استخدمت قدرتك مرة واحدة مسبقاً!", ephemeral=True)
                return
            self.game.investigate_target = target
            self.game.used_powers.add(self.player_id)
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
    def __init__(self, cog: WerewolfCog, game: GameSession, voter_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.game = game
        self.voter_id = voter_id
        targets = [p for p in game.alive if p != voter_id]
        if not targets:
            return
        select = discord.ui.Select(placeholder="اختر من تصلبه")
        for t in targets:
            user = cog.bot.get_user(t)
            if user:
                select.add_option(label=user.display_name, value=str(t))
        select.callback = self.vote_callback
        self.add_item(select)

    async def vote_callback(self, interaction: discord.Interaction):
        target = int(interaction.data["values"][0])
        if self.voter_id in self.game.has_voted:
            await interaction.response.send_message("لقد صوت مسبقاً!", ephemeral=True)
            return
        weight = 2 if self.game.roles[self.voter_id].name_ar == "العمدة" else 1
        self.game.votes[target] = self.game.votes.get(target, 0) + weight
        self.game.has_voted.add(self.voter_id)
        await interaction.response.send_message(f"✅ تم تسجيل صوتك (وزنه {weight}).", ephemeral=True)
        self.stop()

class KingExecutionView(discord.ui.View):
    def __init__(self, cog: WerewolfCog, game: GameSession, king_id: int):
        super().__init__(timeout=30)
        self.cog = cog
        self.game = game
        self.king_id = king_id
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
        channel = self.cog.bot.get_channel(self.game.channel_id)
        await self.cog.execute_king_execution(self.game, target, channel)
        await interaction.response.send_message("تم تنفيذ الأمر الملكي.", ephemeral=True)
        self.stop()

# -------------------------------------------------------------------
# 7. نظام التذاكر
# -------------------------------------------------------------------
class TicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="تجهيز_التيكت", description="تجهيز لوحة التذاكر (للمشرفين)")
    @commands.has_permissions(administrator=True)
    async def setup_ticket(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🎫 الدعم الفني", description="اضغط الزر لفتح تذكرة خاصة.", color=0x3498db)
        view = TicketPanelView()
        await interaction.response.send_message(embed=embed, view=view)

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="فتح تذكرة جديدة", style=discord.ButtonStyle.primary)
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        category = discord.utils.get(interaction.guild.categories, name="تذاكر الدعم")
        if not category:
            category = await interaction.guild.create_category("تذاكر الدعم")
        num = random.randint(1000, 9999)
        ch_name = f"ticket-{interaction.user.name}-{num}"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True)
        }
        ch = await category.create_text_channel(name=ch_name, overwrites=overwrites)
        embed = discord.Embed(title=f"تذكرة #{num}", description=f"مرحباً {interaction.user.mention}، اشرح مشكلتك.", color=0x2ecc71)
        view = TicketControlView(ch.id)
        await ch.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ تم فتح تذكرتك: {ch.mention}", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="إغلاق", style=discord.ButtonStyle.red)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
        await interaction.response.send_message("🔒 تم إغلاق التذكرة.", ephemeral=True)

    @discord.ui.button(label="حذف", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("سيتم الحذف خلال 5 ثوانٍ...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

    @discord.ui.button(label="أرشف", style=discord.ButtonStyle.secondary)
    async def archive(self, interaction: discord.Interaction, button: discord.ui.Button):
        messages = []
        async for msg in interaction.channel.history(limit=200):
            messages.append(f"[{msg.created_at}] {msg.author.name}: {msg.content}")
        log = "\n".join(messages)
        filename = f"transcript-{interaction.channel.name}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(log)
        await interaction.response.send_message(file=discord.File(filename), ephemeral=True)
        os.remove(filename)

# -------------------------------------------------------------------
# 8. البوت الرئيسي مع معالجة الأخطاء
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
        await self.add_cog(TicketCog(self))
        await self.tree.sync()
        print("✅ تم تحميل جميع الأكواد والمزامنة.")

    async def on_ready(self):
        print(f"🤖 {self.user} شغال بكامل قوته! جاهز للعب والتذاكر.")
        print(f"✅ متصل على {len(self.guilds)} سيرفر.")

# -------------------------------------------------------------------
# 9. التشغيل
# -------------------------------------------------------------------
if __name__ == "__main__":
    bot = PremiumBot()
    bot.run(TOKEN)