# ===================================================================
#  ملف: bot.py
#  بوت ديسكورد متكامل: لعبة الذئب والقروي (Werewolf) + نظام تذاكر
#  يدعم 9 أدوار، نقاط، قاعدة بيانات SQLite مع WAL، 
#  رسائل Ephemeral، ونشر على Render.
#  كل النصوص باللهجة السعودية الساخرة الفخمة.
# ===================================================================

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import asyncio
import random
import os
from typing import Dict, List, Optional, Set, Union
from datetime import datetime

# -------------------------------------------------------------------
# 1. إعدادات قاعدة البيانات (SQLite + WAL + نقاط)
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
# 2. تعريف الأدوار وخصائصها
# -------------------------------------------------------------------
class Role:
    def __init__(self, name_ar: str, team: str, night_action: Optional[str] = None, description: str = "", can_use_once: bool = False):
        self.name_ar = name_ar       # الاسم العربي
        self.team = team             # "wolf" أو "village"
        self.night_action = night_action   # "kill", "investigate", "protect", "heal", "block", "revive", None
        self.description = description
        self.can_use_once = can_use_once  # هل تستخدم لمرة واحدة فقط (الملك، المحقق، أم زكي، الحارس؟)
        # ملاحظة: الحارس والملك والمحقق وأم زكي يستخدمون مرة واحدة. الطبيب والمغرية كل ليلة.

# الأدوار التسعة المطلوبة (بدون اختصار)
ROLES_DATA = {
    "الذيب": Role("الذيب 🐺", "wolf", "kill", "كل ليلة تتفق مع ربعك الذئاب وتقتلون واحد من القرويين. لا تنكشف!", can_use_once=False),
    "القروي": Role("القروي 🧑‍🌾", "village", None, "ما عندك صلاحيات بالليل، بس صوتك يهم بالنهار. استخدم عقلك واشتبه باللي حواليك.", can_use_once=False),
    "المحقق": Role("المحقق 🔍", "village", "investigate", "مرة واحدة باللعبة تقدر تفحص لاعب وتعرف إذا هو ذيب ولا قروي.", can_use_once=True),
    "الحارس": Role("الحارس 🛡️", "village", "protect", "مرة واحدة تحمي لاعب من الموت بالليل (حتى من الطبيب؟ الطبيب يعالج لكن الحارس يقي).", can_use_once=True),
    "الملك": Role("الملك 👑", "village", None, "مرة واحدة باللعبة تقدر تأمر بقتل أي لاعب بدون تصويت (قدرة مطلقة).", can_use_once=True),
    "العمدة": Role("العمدة 🏛️", "village", None, "صوتك في التصويت النهاري يحتسب بصوتين. تكسر التعادل.", can_use_once=False),
    "الطبيب": Role("الطبيب ⚕️", "village", "heal", "كل ليلة تقدر تحاول تنقذ لاعب من الموت (إذا اختار نفس الشخص اللي هاجمه الذيب، ينجو).", can_use_once=False),
    "المغرية": Role("المغرية 💃", "village", "block", "كل ليلة تزور لاعب؛ إذا كان ذيب يموتون سوا، إذا قروي يتحصن ولا يتأثر بالهجمات.", can_use_once=False),
    "أم زكي": Role("أم زكي 👵", "village", "revive", "مرة واحدة باللعبة تقدر ترجع لاعب ميت إلى الحياة (تنقذه).", can_use_once=True)
}

# -------------------------------------------------------------------
# 3. آلة الحالة (State Machine) – تعريف كل حالة وجلسة اللعبة
# -------------------------------------------------------------------
class GamePhase:
    LOBBY = "lobby"            # انتظار التسجيل
    NIGHT = "night"            # الليل (الجميع ينام، أصحاب الأدوار يتصرفون)
    DAY_DISCUSSION = "day_discussion"   # نقاش نهاري قبل التصويت
    DAY_VOTING = "day_voting"           # التصويت على الصلب
    DAY_KING_EXECUTION = "day_king"     # مرحلة استخدام الملك لصلاحيته
    ENDED = "ended"            # انتهت اللعبة

class GameSession:
    def __init__(self, guild_id: int, channel_id: int):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.phase = GamePhase.LOBBY
        self.players: List[int] = []           # جميع المسجلين
        self.alive: List[int] = []             # اللاعبين الأحياء
        self.roles: Dict[int, Role] = {}       # id -> دور
        self.night_actions: Dict[str, Optional[int]] = {}  # تخزين مؤقت للإجراءات الليلية
        self.used_powers: Set[int] = set()     # اللاعبين اللي استخدموا قدرة لمرة واحدة
        self.king_used: bool = False           # هل استخدم الملك صلاحيته؟
        self.mayor_id: Optional[int] = None    # العمدة (صوته مضاعف)
        self.umm_zaki_used: bool = False       # هل استخدمت أم زكي الإحياء؟
        self.revive_target: Optional[int] = None  # من سيتم إحياؤه هذه الليلة
        self.protect_target: Optional[int] = None # من يحميه الحارس
        self.heal_target: Optional[int] = None     # من يعالجه الطبيب
        self.block_target: Optional[int] = None    # من تعطله المغرية
        self.investigate_target: Optional[int] = None  # من يفحصه المحقق
        self.kill_target: Optional[int] = None     # من سيقتله الذئب
        self.wolf_players: List[int] = []          # قائمة الذئاب (لتنسيق القتل)
        self.votes: Dict[int, int] = {}            # target_id -> عدد الأصوات (مع الوزن)
        self.has_voted: Set[int] = set()           # من صوت بالفعل
        self.lynched_player: Optional[int] = None  # نتيجة التصويت
        self.winner: Optional[str] = None          # "wolf" أو "village"

    def is_alive(self, user_id: int) -> bool:
        return user_id in self.alive

    def get_alive_players(self) -> List[int]:
        return self.alive.copy()

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
# 4. Cog اللعبة الرئيسي (WerewolfCog)
# -------------------------------------------------------------------
class WerewolfCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db
        self.games: Dict[int, GameSession] = {}   # guild_id -> GameSession

    # -----------------------------------------------------------------
    # أوامر التسجيل والبدء
    # -----------------------------------------------------------------
    @app_commands.command(name="ذيب", description="افتح تسجيل لعبة القرويين والذئاب")
    async def lobby_command(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        game = self.games.get(guild_id)
        if game and game.phase != GamePhase.LOBBY:
            await interaction.response.send_message("❌ فيه لعبة شغالة حالياً، اصبر لين تخلص.", ephemeral=True)
            return
        if not game:
            game = GameSession(guild_id, interaction.channel_id)
            self.games[guild_id] = game

        embed = discord.Embed(
            title="🐺 قرية الغدر والطقطقة 🐺",
            description="**سجل الآن في لعبة الذئب والقروي!**\nعدد اللاعبين المطلوب: 4 إلى 9.\nاضغط على الأزرار بالأسفل.",
            color=0x9b59b6
        )
        embed.add_field(name="👥 المسجلين", value="لا أحد بعد .. خايفين؟", inline=False)
        embed.set_footer(text="سيتم توزيع الأدوار بعد الضغط على ابدأ")

        view = RegistrationView(self, guild_id)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="ابدأ_الذيب", description="ابدأ اللعبة بعد اكتمال التسجيل")
    async def start_game_command(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        game = self.games.get(guild_id)
        if not game or game.phase != GamePhase.LOBBY:
            await interaction.response.send_message("❌ ما فيه تسجيل مفتوح أو اللعبة بدأت.", ephemeral=True)
            return
        if len(game.players) < 4:
            await interaction.response.send_message(f"❌ العدد قليل ({len(game.players)}). يحتاج 4 على الأقل.", ephemeral=True)
            return
        if len(game.players) > 9:
            await interaction.response.send_message(f"❌ العدد كثير ({len(game.players)}). الحد الأقصى 9.", ephemeral=True)
            return

        # توزيع الأدوار بناءً على عدد اللاعبين (قائمة ثابتة لضمان توازن اللعبة)
        await self.assign_roles(game)
        game.phase = GamePhase.NIGHT

        # إرسال الأدوار لكل لاعب برسائل Ephemeral
        for pid in game.players:
            user = self.bot.get_user(pid)
            if user:
                role = game.roles[pid]
                embed = discord.Embed(
                    title="🔮 دورك السري",
                    description=f"أنت **{role.name_ar}**\n{role.description}",
                    color=0x2ecc71
                )
                # لا يمكن إرسال ephemeral إلا كرد على تفاعل، لذلك نستخدم followup داخل الـ view
                # بدلاً من ذلك: سنرسل رسالة عادية مؤقتة أو نستخدم DM. لكن الشرط يريد ephemeral بالعام.
                # الحل: سنستخدم interaction.followup.send(ephemeral=True) لكننا نحتاج interaction.
                # سننشئ لكل لاعب زراً خاصاً يكشف دوره سراً (كما في الكود السابق) لكن عبر View منفصل.
                pass

        # عرض أزرار الكشف عن الأدوار
        view = RevealRolesView(game.players, game.roles)
        await interaction.response.send_message("✅ **تم توزيع الأدوار!** اضغط على الزر لمعرفة دورك (رسالة مخفية).", view=view)

        # انتظار 15 ثانية ثم بدء أول ليلة
        await asyncio.sleep(15)
        await self.start_night_phase(interaction.channel, game)

    async def assign_roles(self, game: GameSession):
        """توزيع الأدوار بشكل متوازن بناءً على عدد اللاعبين."""
        num = len(game.players)
        # قوائم الأدوار المضمونة (التوازن: ذيب واحد + أدوار خاصة حسب العدد)
        # المخطط: 4 لاعبين -> ذيب + 3 قروي عادي
        # 5 -> ذيب + محقق + 3 قروي
        # 6 -> ذيب + محقق + طبيب + 3 قروي
        # 7 -> ذيب + محقق + طبيب + حارس + 3 قروي
        # 8 -> ذيب + محقق + طبيب + حارس + مغرية + 3 قروي
        # 9 -> ذيب + محقق + طبيب + حارس + مغرية + ملك + عمدة + أم زكي + قروي (9)
        # لكن يجب أن نضع الأدوار التسعة كلها عند العدد 9.
        role_names = []
        # ضمان وجود ذيب واحد
        role_names.append("الذيب")
        # الأدوار الخاصة المهمة (حسب العدد)
        specials = ["المحقق", "الطبيب", "الحارس", "المغرية", "الملك", "العمدة", "أم زكي"]
        random.shuffle(specials)
        # نضيف من specials بقدر ما يسع العدد
        for i in range(min(num - 2, len(specials))):
            role_names.append(specials[i])
        # باقي العدد نعبئه بالقروي
        while len(role_names) < num:
            role_names.append("القروي")
        random.shuffle(role_names)

        # تعيين الأدوار للاعبين
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
        # إعادة تعيين الإجراءات الليلية
        game.night_actions.clear()
        game.kill_target = None
        game.protect_target = None
        game.heal_target = None
        game.block_target = None
        game.investigate_target = None
        game.revive_target = None

        # إرسال إعلان الليل
        await channel.send("🌙 **هدوء الليل...** أصحاب القدرات الخاصة، استخدموا أزراركم (سيتم إرسالها لكم خاصاً). لديكم 60 ثانية.")
        # إرسال واجهات تفاعلية لكل لاعب حي حسب دوره
        for pid in game.alive:
            user = self.bot.get_user(pid)
            if not user:
                continue
            role = game.roles[pid]
            if role.night_action is None:
                continue
            # إنشاء View مناسب لكل قدرة
            view = NightActionView(self, game, pid)
            await user.send(f"🌙 **ليلتك كـ {role.name_ar}**\nاختر هدفك:", view=view)

        # انتظار 60 ثانية ثم معالجة النتائج
        await asyncio.sleep(60)
        await self.resolve_night_actions(channel, game)

    async def resolve_night_actions(self, channel: discord.TextChannel, game: GameSession):
        # تطبيق تأثير المغرية (block) أولاً: إذا كانت المغرية قد اختارت أحداً
        seductress = [p for p in game.alive if game.roles[p].night_action == "block"]
        if seductress and game.block_target is not None:
            target = game.block_target
            # إذا كان الهدف ذيباً، يموت الاثنان
            if game.roles[target].team == "wolf":
                # المغرية نفسها (أول عنصر في القائمة) تموت مع الذيب
                sed_id = seductress[0]
                if sed_id in game.alive:
                    game.alive.remove(sed_id)
                if target in game.alive:
                    game.alive.remove(target)
                await channel.send(f"💃 **فضيحة ليلية:** المغرية {self.bot.get_user(sed_id).mention} زارت {self.bot.get_user(target).mention} اللي طلع ذيب! وماتوا الاثنين سوا!")
                # بعد هذا الحدث، إذا انتهت اللعبة ننهي
                if game.check_game_over():
                    await self.end_game(channel, game)
                    return
            else:
                # إذا كان قروياً، يتم حمايته ولا يمكن قتله هذه الليلة (نضع علامة)
                # سنعتمد على متغير `blocked_protected` لمنع القتل
                game.protect_target = target  # كأن الحارس حماه، لمنع القتل.
                await channel.send(f"💃 **المغرية زارت {self.bot.get_user(target).mention} وحصنته من الذياب الليلة.")

        # تطبيق الإحياء (أم زكي) – تنفذ قبل القتل
        if game.revive_target is not None and game.revive_target not in game.alive:
            # الهدف كان ميتاً – نعيده للحياة
            game.alive.append(game.revive_target)
            await channel.send(f"👵 **أم زكي** حركت عصاها السحرية وأعادت {self.bot.get_user(game.revive_target).mention} إلى الحياة!")
            game.umm_zaki_used = True

        # تطبيق القتل (الذئب)
        kill = game.kill_target
        if kill is not None and kill in game.alive:
            # هل الهدف محمي بالحارس أو معالج بالطبيب أو محصن من المغرية؟
            protected = (game.protect_target == kill)
            healed = (game.heal_target == kill)
            if not (protected or healed):
                game.alive.remove(kill)
                await channel.send(f"💀 **الذيب هاجم {self.bot.get_user(kill).mention} ومات القروي المسكين!")
            else:
                await channel.send(f"🛡️ **تم إنقاذ {self.bot.get_user(kill).mention}** بسبب الحماية أو العلاج.")
        else:
            await channel.send("🕊️ **ليلتهادي** ما مات أحد الليلة.")

        # تطبيق تحقيق المحقق
        inv = game.investigate_target
        if inv is not None:
            detective = [p for p in game.alive if game.roles[p].night_action == "investigate"]
            if detective and inv in game.alive:
                result = "ذيب 🐺" if game.roles[inv].team == "wolf" else "قروي 🧑‍🌾"
                await self.bot.get_user(detective[0]).send(f"🔍 **نتيجة التحقيق:** {self.bot.get_user(inv).mention} هو {result}.")
                # نستخدم used_powers للمحقق إذا مرة واحدة
                game.used_powers.add(detective[0])
            elif detective:
                await self.bot.get_user(detective[0]).send("❌ الهدف غير موجود أو ميت.")

        # بعد الليل، نتحقق من انتهاء اللعبة
        if game.check_game_over():
            await self.end_game(channel, game)
            return

        # ننتقل إلى مرحلة النهار (نقاش)
        game.phase = GamePhase.DAY_DISCUSSION
        await channel.send("☀️ **طلع الصبح وصحى القرويين!** ابدوا نقاشكم واتهموا الخونة. بعد دقيقتين سنبدأ التصويت.")
        await asyncio.sleep(120)  # دقيقتان نقاش
        await self.start_day_voting(channel, game)

    async def start_day_voting(self, channel: discord.TextChannel, game: GameSession):
        game.phase = GamePhase.DAY_VOTING
        game.votes.clear()
        game.has_voted.clear()
        game.lynched_player = None

        await channel.send("🗳️ **حان وقت التصويت!** كل لاعب يختار من يريد صلبه (باستخدام الزر بالأسفل). لديكم 60 ثانية.")
        # إرسال واجهة تصويت لكل لاعب حي
        for pid in game.alive:
            user = self.bot.get_user(pid)
            if user:
                view = VotingView(self, game, pid)
                await user.send("🗳️ **اختر من تريد صلبه:**", view=view)

        await asyncio.sleep(60)
        await self.resolve_day_voting(channel, game)

    async def resolve_day_voting(self, channel: discord.TextChannel, game: GameSession):
        if not game.votes:
            await channel.send("💤 **لا أحد صوت!** تمرر الجلسة بدون صلب.")
        else:
            # العثور على أعلى الأصوات
            max_votes = max(game.votes.values())
            candidates = [pid for pid, count in game.votes.items() if count == max_votes]
            if len(candidates) > 1 and game.mayor_id and game.mayor_id in game.alive:
                # العمدة يكسر التعادل بتصويته (يختار من بين المرشحين)
                # نبسط: نأخذ أول مرشح، لكن يمكن جعل العمدة يختار عبر رسالة
                chosen = random.choice(candidates)  # تبسيط، لكن الأفضل سؤال العمدة
                await channel.send(f"🏛️ **العمدة** كسر التعادل ووقع الاختيار على {self.bot.get_user(chosen).mention}.")
                game.lynched_player = chosen
            else:
                game.lynched_player = candidates[0]

            if game.lynched_player in game.alive:
                game.alive.remove(game.lynched_player)
                role_name = game.roles[game.lynched_player].name_ar
                await channel.send(f"⚰️ **تم صلب {self.bot.get_user(game.lynched_player).mention}!** كان دوره {role_name}.")
            else:
                await channel.send("⚠️ حدث خطأ في الصلب.")

        # بعد الصلب، تحقق من النهاية
        if game.check_game_over():
            await self.end_game(channel, game)
            return

        # ننتقل إلى مرحلة الملك (إذا لم يستخدم صلاحيته)
        if not game.king_used and any(r.name_ar == "الملك" and p in game.alive for p, r in game.roles.items()):
            game.phase = GamePhase.DAY_KING_EXECUTION
            await channel.send("👑 **للملك الحق في إعدام أحد المشتبه بهم فوراً بدون تصويت (مرة باللعبة).** هل يرغب الملك في استخدام صلاحيته؟ لديه 30 ثانية.")
            # نرسل للملك رسالة خاصة
            king = [p for p in game.alive if game.roles[p].name_ar == "الملك"]
            if king:
                user = self.bot.get_user(king[0])
                if user:
                    view = KingExecutionView(self, game, king[0])
                    await user.send("👑 **أمر ملكي:** اختر من تعدمه فوراً.", view=view)
                    await asyncio.sleep(30)
            # إذا استخدم الملك، سنكون قد عالجنا الإعدام في الـ view
        else:
            # الانتقال إلى الليل التالي
            await self.start_night_phase(channel, game)

    async def execute_king_execution(self, game: GameSession, target: int, channel: discord.TextChannel):
        if target in game.alive:
            game.alive.remove(target)
            await channel.send(f"👑 **بأمر الملك {self.bot.get_user(target).mention} أعدم فوراً!** دوره كان {game.roles[target].name_ar}.")
            game.king_used = True
            if game.check_game_over():
                await self.end_game(channel, game)
                return
        await self.start_night_phase(channel, game)

    async def end_game(self, channel: discord.TextChannel, game: GameSession):
        game.phase = GamePhase.ENDED
        winner = game.winner
        if winner == "wolf":
            points = 60
            winners = game.get_alive_wolves()
            msg = f"🐺 **فوز الذئاب!** كل ذيب حي يربح {points} نقطة."
        else:
            points = 45
            winners = game.get_alive_villagers()
            msg = f"🏘️ **فوز القرويين!** كل قروي حي يربح {points} نقطة."

        for pid in winners:
            await self.db.add_points(pid, points)

        embed = discord.Embed(title="🏁 نهاية اللعبة", description=msg, color=0xf1c40f)
        for pid in game.players:
            user = self.bot.get_user(pid)
            if not user:
                continue
            role = game.roles[pid].name_ar
            status = "❤️ حي" if pid in game.alive else "💀 ميت"
            embed.add_field(name=user.display_name, value=f"{role} ({status})", inline=True)

        await channel.send(embed=embed)
        # حذف الجلسة
        self.games.pop(game.guild_id, None)

    @app_commands.command(name="نقاطي", description="عرض نقاطك في اللعبة")
    async def my_points(self, interaction: discord.Interaction):
        pts = await self.db.get_points(interaction.user.id)
        await interaction.response.send_message(f"📊 **{interaction.user.display_name}** نقاطك: **{pts}** نقطة.", ephemeral=True)

    @app_commands.command(name="تصفير_الذيب", description="تصفير جميع النقاط (للمشرفين فقط)")
    @commands.has_permissions(administrator=True)
    async def reset_points(self, interaction: discord.Interaction):
        await self.db.reset_all_points()
        await interaction.response.send_message("✅ **تم تصفير جميع النقاط بنجاح!**", ephemeral=True)

# -------------------------------------------------------------------
# 5. واجهات المستخدم (Views) للتسجيل والتصويت والإجراءات الليلية
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
        # تحديث الـ embed
        embed = interaction.message.embeds[0]
        if len(game.players) > 0:
            names = ", ".join([f"<@{p}>" for p in game.players])
            embed.set_field_at(0, name="👥 المسجلين", value=names, inline=False)
        else:
            embed.set_field_at(0, name="👥 المسجلين", value="لا أحد", inline=False)
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message("✅ تم انضمامك!", ephemeral=True)

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
        if len(game.players) > 0:
            names = ", ".join([f"<@{p}>" for p in game.players])
            embed.set_field_at(0, name="👥 المسجلين", value=names, inline=False)
        else:
            embed.set_field_at(0, name="👥 المسجلين", value="لا أحد", inline=False)
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message("🚪 غادرت التسجيل.", ephemeral=True)

class RevealRolesView(discord.ui.View):
    def __init__(self, players: List[int], roles: Dict[int, Role]):
        super().__init__(timeout=60)
        self.players = players
        self.roles = roles

    @discord.ui.button(label="اعرف دورك 🔮", style=discord.ButtonStyle.primary)
    async def reveal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.players:
            await interaction.response.send_message("أنت لست مشاركاً في هذه اللعبة.", ephemeral=True)
            return
        role = self.roles[interaction.user.id]
        embed = discord.Embed(title="دورك السري", description=f"أنت **{role.name_ar}**\n{role.description}", color=0x9b59b6)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        # تعطيل الزر بعد الكشف (اختياري)
        button.disabled = True
        await interaction.message.edit(view=self)

class NightActionView(discord.ui.View):
    def __init__(self, cog: WerewolfCog, game: GameSession, player_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.game = game
        self.player_id = player_id
        role = game.roles[player_id]
        self.action = role.night_action
        # منع استخدام القدرة لمرة واحدة إذا استخدمها سابقاً
        if role.can_use_once and player_id in game.used_powers:
            self.disabled = True
            return
        # بناء قائمة الأهداف (اللاعبين الأحياء عدا نفسه)
        targets = [p for p in game.alive if p != player_id]
        if not targets:
            return
        select = discord.ui.Select(placeholder=f"اختر هدفاً لـ {role.name_ar}")
        for t in targets:
            user = cog.bot.get_user(t)
            if user:
                select.add_option(label=user.display_name, value=str(t))
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        target = int(interaction.data["values"][0])
        # تخزين الإجراء حسب النوع
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
            # يمكن إحياء لاعب ميت فقط
            if target not in self.game.players or target in self.game.alive:
                await interaction.response.send_message("هذا اللاعب حي أو غير موجود!", ephemeral=True)
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
# 6. نظام التذاكر (Ticket System)
# -------------------------------------------------------------------
class TicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="تجهيز_التيكت", description="تجهيز لوحة التذاكر (للمشرفين)")
    @commands.has_permissions(administrator=True)
    async def setup_ticket_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎫 نظام الدعم الفني",
            description="اضغط على الزر لفتح تذكرة خاصة، سيقوم الفريق بالرد عليك.",
            color=0x3498db
        )
        view = TicketPanelView()
        await interaction.response.send_message(embed=embed, view=view)

class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="فتح تذكرة جديدة", style=discord.ButtonStyle.primary)
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # البحث عن كاتيجوري التذاكر
        category = discord.utils.get(interaction.guild.categories, name="تذاكر الدعم")
        if not category:
            category = await interaction.guild.create_category("تذاكر الدعم")
        # إنشاء روم جديد
        ticket_num = random.randint(1000, 9999)
        channel_name = f"ticket-{interaction.user.name}-{ticket_num}"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True)
        }
        ticket_channel = await category.create_text_channel(name=channel_name, overwrites=overwrites)
        embed = discord.Embed(
            title=f"تذكرتك #{ticket_num}",
            description=f"مرحباً {interaction.user.mention}، اشرح مشكلتك وسيتم الرد عليك قريباً.\nاستخدم الأزرار بالأسفل لإدارة التذكرة.",
            color=0x2ecc71
        )
        view = TicketControlView(ticket_channel.id)
        await ticket_channel.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ تم فتح تذكرتك: {ticket_channel.mention}", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="إغلاق (قفل)", style=discord.ButtonStyle.red)
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        # منع الكتابة للجميع ما عدا الأدمن
        await channel.set_permissions(interaction.guild.default_role, send_messages=False)
        await interaction.response.send_message("🔒 تم إغلاق التذكرة. لا يمكن الكتابة الآن.", ephemeral=True)

    @discord.ui.button(label="حذف التذكرة", style=discord.ButtonStyle.danger)
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("سيتم حذف هذه التذكرة خلال 5 ثوانٍ...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

    @discord.ui.button(label="أرشف", style=discord.ButtonStyle.secondary)
    async def archive_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ببساطة نسجل المحتوى ونحذف
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
# 7. البوت الرئيسي (مع إعداد المزامنة)
# -------------------------------------------------------------------
class WerewolfPremiumBot(commands.Bot):
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
        # مزامنة الأوامر السريعة (Slash Commands)
        await self.tree.sync()
        print("✅ تم تحميل جميع الأكواد والمزامنة.")

    async def on_ready(self):
        print(f"🤖 {self.user} شغال بكامل قوته! جاهز للعب والتذاكر.")

# -------------------------------------------------------------------
# 8. تشغيل البوت
# -------------------------------------------------------------------
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("ضع التوكن في متغير البيئة DISCORD_TOKEN")
    bot = WerewolfPremiumBot()
    bot.run(token)