
import os
import random
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List, Tuple
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ==============================================================================
# 1. 
# ==============================================================================

class DatabaseManager:
    def init(self, db_path: str = "werewolf.db"):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        """إنشاء الجداول وتفعيل نمط WAL لضمان سرعة القراءة والكتابة المتزامنة"""
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self._create_tables()

    async def _create_tables(self):
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS game_sessions (
                guild_id INTEGER PRIMARY KEY,
                game_phase TEXT NOT NULL,
                day_number INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS game_players (
                guild_id INTEGER,
                player_id INTEGER,
                assigned_role TEXT NOT NULL,
                is_alive BOOLEAN DEFAULT 1,
                has_used_power BOOLEAN DEFAULT 0,
                display_name TEXT,
                PRIMARY KEY (guild_id, player_id)
            )
        """)
        await self.conn.commit()

    async def save_game_state(self, guild_id: int, game_phase: str, players: Dict[int, Dict], day_number: int = 0):
        if not self.conn:
            return
        async with self.conn.execute("BEGIN"):
            await self.conn.execute("""
                INSERT OR REPLACE INTO game_sessions (guild_id, game_phase, day_number)
                VALUES (?, ?, ?)
            """, (guild_id, game_phase, day_number))

            await self.conn.execute("DELETE FROM game_players WHERE guild_id = ?", (guild_id,))

            for pid, data in players.items():
                await self.conn.execute("""
                    INSERT INTO game_players (guild_id, player_id, assigned_role, is_alive, has_used_power, display_name)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (guild_id, pid, data['role'], 1 if data['alive'] else 0, 1 if data.get('has_used_power', False) else 0, data.get('display_name', 'لاعب')))
        await self.conn.commit()

    async def load_game_state(self, guild_id: int) -> Tuple[Optional[str], int, Dict[int, Dict]]:
        if not self.conn:
            return None, 0, {}
        cursor = await self.conn.execute(
            "SELECT game_phase, day_number FROM game_sessions WHERE guild_id = ?", (guild_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None, 0, {}
        game_phase, day_number = row

        players = {}
        async with self.conn.execute(
            "SELECT player_id, assigned_role, is_alive, has_used_power, display_name FROM game_players WHERE guild_id = ?",
            (guild_id,)
        ) as cursor:
            async for pid, role, alive, used_power, disp_name in cursor:
                players[pid] = {
                    'role': role,
                    'alive': bool(alive),
                    'has_used_power': bool(used_power),
                    'display_name': disp_name,
                    'night_flags': {}
                }
        return game_phase, day_number, players

    async def update_player_status(self, guild_id: int, player_id: int, **kwargs):

> Yasser:
if not self.conn:
            return
        valid_fields = {'is_alive', 'has_used_power', 'assigned_role', 'display_name'}
        updates = []
        values = []
        for field, value in kwargs.items():
            if field not in valid_fields:
                raise ValueError(f"الحقل غير صالح: {field}")
            updates.append(f"{field} = ?")
            if isinstance(value, bool):
                value = 1 if value else 0
            values.append(value)

        if not updates:
            return

        values.extend([guild_id, player_id])
        query = f"UPDATE game_players SET {', '.join(updates)} WHERE guild_id = ? AND player_id = ?"
        await self.conn.execute(query, values)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

db = DatabaseManager()

# ==============================================================================
# 2. الهياكل البيانية والبيانات العامة 
# ==============================================================================

ROLES_INFO = {
    "الذيب": "🐺 يحاول التخلص من الجميع والسيطرة على اللعبة بالكامل. (عشائكم اليوم حواوشي!)",
    "القروي": "🧑‍🌾 شخصية عادية، ما عندك قدرة خاصة بس شارك بالتصويت واكشف الذيابة بذكائك.",
    "المحقق": "🔍 تقدر تكشف هوية أي لاعب (مرة واحدة فقط) طوال القيم.",
    "الحارس": "🛡️ تعطي درع حماية لأي لاعب وتحميه من القتل (مرة واحدة فقط).",
    "الملك": "👑 تملك سلطة تحويل جميع الأصوات على لاعب واحد وطرده مباشرة (مرة واحدة فقط).",
    "العمدة": "🏛️ صوتك أقوى من الجميع، وصوتك بالانتخابات ينحسب بصوتين!",
    "الطبيب": "⚕️ تستطيع حماية أي لاعب من القتل كل ليلة (اختر بحذر!).",
    "المغرية": "💃 إذا زرتي ذيب تموتين معه، وإذا هجمت الذيابة على شخص عادي تحمينه.",
    "أم فادي": "👵 إذا قتلوها الذيابة، تقوم بفضح أحد الذيابة وتكشف اسمه للسيرفر قبل ما تموت!"
}

ROLE_ALIASES = {
    "الذيب": {"wolf", "w", "الذيب", "ذيب", "ذئب"},
    "المحقق": {"detective", "المحقق", "محقق"},
    "الحارس": {"guard", "الحارس", "حارس"},
    "الطبيب": {"doctor", "الطبيب", "طبيب"},
    "المغرية": {"seducer", "المغرية", "مغرية"},
    "أم فادي": {"um_fadi", "ام فادي", "أم فادي", "um fadi"},
    "الملك": {"king", "الملك", "ملك"},
    "العمدة": {"mayor", "العمدة", "عمدة"},
    "القروي": {"villager", "villager ", "القرية", "مدني", "القروي", "قروي"},
}

def normalize_role(role_text: str) -> str:
    text = str(role_text or "").strip().lower()
    for official_name, aliases in ROLE_ALIASES.items():
        if text in aliases:
            return official_name
    return text

class GameInstance:
    def init(self, guild_id: int):
        self.guild_id = guild_id
        self.game_phase = "signup"  # signup, night, day, ended
        self.day_number = 0
        self.players: Dict[int, Dict[str, Any]] = {}  # user_id -> {role, alive, display_name, night_flags}
        self.alive_players: List[int] = []
        self.game_started = False
        self.channel_id: Optional[int] = None

    @classmethod
    def from_state(cls, guild_id: int, phase: str, day_num: int, players_data: Dict[int, Dict]):
        instance = cls(guild_id)
        instance.game_phase = phase
        instance.day_number = day_num
        instance.players = players_data
        instance.alive_players = [pid for pid, p in players_data.items() if p['alive']]
        instance.game_started = phase != 'signup' and phase != 'ended'
        return instance

@dataclass
class NightTargets:
    detective_target: Optional[int] = None
    guard_target: Optional[int] = None
    doctor_target: Optional[int] = None
    seducer_target: Optional[int] = None
    wolves_target: Optional[int] = None
    protected_targets: set[int] = field(default_factory=set)
    deaths: dict[int, str] = field(default_factory=dict)
    seducer_died_with_wolf: bool = False

> Yasser:
@dataclass
class VoteSession:
    eligible_voters: set[int] = field(default_factory=set)
    ballots: dict[int, int] = field(default_factory=dict)  # voter_id -> target_id
    force_target: Optional[int] = None
    force_used: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record_vote(self, voter_id: int, target_id: int) -> None:
        async with self.lock:
            self.ballots[voter_id] = target_id
            if self.eligible_voters.issubset(self.ballots.keys()):
                self.finished.set()

    async def force_lynch(self, target_id: int) -> bool:
        async with self.lock:
            if self.force_used:
                return False
            self.force_used = True
            self.force_target = target_id
            self.finished.set()
            return True

# الخريطة العالمية لحفظ ألعاب السيرفرات النشطة في الذاكرة
games: Dict[int, GameInstance] = {}

# ==============================================================================
# 3. واجهات التصميم الاحترافي والرسائل والردود  
# ==============================================================================

def embed_night_announcement() -> discord.Embed:
    embed = discord.Embed(
        title="🌙 الليل يخيّم على القرية",
        description="أغمضوا أعينكم... الذياب خرجوا للصيد 🐺\n\nكل الأدوار الخاصة، نفذوا أفعالكم في الخاص الآن عبر الرسائل المستلمة.",
        color=0x2c3e50
    )
    embed.set_thumbnail(url="https://i.imgur.com/8z3kL9p.png")
    embed.set_footer(text="الليل آمن... لمن ينجو")
    return embed

def embed_morning_no_death() -> discord.Embed:
    embed = discord.Embed(
        title="☀️ بزوغ الفجر",
        description="🌅 أشرقت الشمس... وما زال الجميع على قيد الحياة!",
        color=0xf1c40f
    )
    embed.add_field(name="الوضع الحالي", value="القرية هادئة... لكن التوتر في تصاعد 👀", inline=False)
    embed.set_footer(text="هل كان هناك حارس أو طبيب يقظ؟")
    return embed

def embed_morning_death(player_name: str, role: str) -> discord.Embed:
    death_messages = [
        f"🩸 {player_name} وُجد مقتولاً في منزله...",
        f"💀 صرخات {player_name} دوّت في أرجاء القرية...",
        f"🐺 الذياب أكلوا {player_name} على العشاء!"
    ]
    embed = discord.Embed(
        title="☀️ اكتشاف الجثة",
        description=random.choice(death_messages),
        color=0xc0392b
    )
    embed.add_field(name="الضحية", value=player_name, inline=True)
    embed.add_field(name="الدور الحقيقي", value=f"{role}", inline=True)
    embed.add_field(name="الوضع", value="القرية في حالة ذعر...", inline=False)
    return embed

def embed_wolves_victory() -> discord.Embed:
    embed = discord.Embed(
        title="🐺 انتصار الذياب",
        description="الذياب سيطروا على القرية...\nلم يبقَ إلا العظام والدماء.",
        color=0x8B0000
    )
    embed.add_field(name="النتيجة", value="الذياب فازوا بالكامل 🏆", inline=False)
    return embed

def embed_villagers_victory() -> discord.Embed:
    embed = discord.Embed(
        title="🧑‍🌾 انتصار القرويين",
        description="تم القضاء على آخر ذيب...\nالقرية آمنة ومستقرة مرة أخرى!",
        color=0x27ae60
    )
    embed.add_field(name="النتيجة", value="القرويين فازوا بالبطولة 🏆", inline=False)
    embed.set_footer(text="أم زكي سعيدة جداً اليوم 👵")
    return embed

async def fire_already_used_power(interaction: discord.Interaction, role: str):
    roasts = [
        f"يا {interaction.user.mention}، قدرة {role} تُستخدم مرة واحدة بس! تبي تكسر القوانين؟ 😂",
        f"استخدمت قوتك وخلاص.. الحين تبي كرت ثاني؟ تعلّ م ونم يا بطل",
        f"{role} خلصت يا ذكي، روح العب كقروي عادي واستمتع بالصمت",
        "يا أخي حتى في لعبة الذيب تبي تطمع وتغش؟ 😭"
    ]
    await interaction.response.send_message(random.choice(roasts), ephemeral=True)

async def fire_not_your_turn(interaction: discord.Interaction):
    await interaction.response.send_message("ههههه... انتظر دورك الفعلي يا عجول!", ephemeral=True)

> Yasser:
async def fire_not_in_game(interaction: discord.Interaction):
    await interaction.response.send_message("أنت مو داخل القيم أصلاً يا شبح 👻", ephemeral=True)

# ==============================================================================
# 4. نظام القوائم التفاعلية والصفحات (ChatGPT Paged Target Views)
# ==============================================================================

class BasePagedTargetView(discord.ui.View):
    def init(
        self,
        *,
        author_id: int,
        candidates: List[Tuple[int, str]],
        title: str,
        prompt: str,
        timeout: int = 75,
        page_size: int = 20,
        allow_cancel: bool = True,
        confirm_label: str = "تأكيد القرار",
        cancel_label: str = "إلغاء",
    ) -> None:
        super().init(timeout=timeout)
        self.author_id = author_id
        self.candidates = candidates[:]
        self.title = title
        self.prompt = prompt
        self.page_size = max(5, min(page_size, 24))
        self.allow_cancel = allow_cancel

        self.page = 0
        self.selected_id: Optional[int] = None
        self.result: Optional[int] = None
        self.message: Optional[discord.Message] = None

        self._build_components(confirm_label=confirm_label, cancel_label=cancel_label)

    def _page_count(self) -> int:
        if not self.candidates:
            return 1
        return (len(self.candidates) + self.page_size - 1) // self.page_size

    def _page_slice(self) -> List[Tuple[int, str]]:
        start = self.page * self.page_size
        end = start + self.page_size
        return self.candidates[start:end]

    def _current_options(self) -> List[discord.SelectOption]:
        options: List[discord.SelectOption] = []
        for uid, label in self._page_slice():
            options.append(discord.SelectOption(label=label[:100], value=str(uid)))
        if not options:
            options = [discord.SelectOption(label="لا توجد أهداف متاحة حالياً", value="0")]
        return options

    def _build_components(self, *, confirm_label: str, cancel_label: str) -> None:
        self.clear_items()

        self.select = discord.ui.Select(
            placeholder=self._select_placeholder(),
            min_values=1,
            max_values=1,
            options=self._current_options(),
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        self.prev_button = discord.ui.Button(label="السابق", style=discord.ButtonStyle.secondary, disabled=self.page <= 0)
        self.prev_button.callback = self._on_prev
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(label="التالي", style=discord.ButtonStyle.secondary, disabled=self.page >= self._page_count() - 1)
        self.next_button.callback = self._on_next
        self.add_item(self.next_button)

        self.confirm_button = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.success)
        self.confirm_button.callback = self._on_confirm
        self.add_item(self.confirm_button)

        if self.allow_cancel:
            self.cancel_button = discord.ui.Button(label=cancel_label, style=discord.ButtonStyle.danger)
            self.cancel_button.callback = self._on_cancel
            self.add_item(self.cancel_button)

    def _select_placeholder(self) -> str:
        if self._page_count() > 1:
            return f"اختر هدفك السري... (صفحة {self.page + 1}/{self._page_count()})"
        return "اختر من القائمة المتاحة..."

    def _selected_label(self, uid: int) -> str:
        for candidate_uid, label in self.candidates:
            if candidate_uid == uid:
                return label
        return "هدف غير مدرج"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("هذا الاختيار خاص ومقيد للاعب آخر.", ephemeral=True)
            return False
        return True

> Yasser:
async def _refresh(self, interaction: discord.Interaction, note: Optional[str] = None) -> None:
        self.select.options = self._current_options()
        self.select.placeholder = self._select_placeholder()
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self._page_count() - 1

        content = self._render_content(note=note)
        await interaction.response.edit_message(content=content, view=self)

    def _render_content(self, note: Optional[str] = None) -> str:
        lines = [f"{self.title}", self.prompt]
        if self.selected_id is not None:
            lines.append(f"الاختيار الحالي ركّز: {self._selected_label(self.selected_id)}")
        if note:
            lines.append(note)
        return "\n".join(lines)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        value = self.select.values[0]
        if value == "0":
            self.selected_id = None
            await self._refresh(interaction, note="لا توجد أهداف في الصفحة الحالية.")
            return
        self.selected_id = int(value)
        await self._refresh(interaction, note=f"تم تحديد: {self._selected_label(self.selected_id)}. اضغط تأكيد لتثبيت الفعل.")

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if self.page > 0:
            self.page -= 1
        await self._refresh(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if self.page < self._page_count() - 1:
            self.page += 1
        await self._refresh(interaction)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        if self.selected_id is None:
            await interaction.response.send_message("يجب عليك اختيار لاعب أولاً قبل الحسم الفعلي.", ephemeral=True)
            return
        self.result = self.selected_id
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=f"{self._render_content()}\n\n✅ تم تثبيت الاختيار بنجاح وإغلاق القائمة.", view=self)
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.result = None
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=f"{self._render_content()}\n\n⛔ تم إلغاء الإجراء الفوري للعملية.", view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(content=f"{self._render_content()}\n\n⌛ انتهت المهلة الزمنية للاستجابة.", view=self)
            except discord.HTTPException:
                pass
        self.stop()

class KingForceVoteView(BasePagedTargetView):
    def init(self, *, author_id: int, candidates: List[Tuple[int, str]], title: str, prompt: str, timeout: int = 75, page_size: int = 20, allow_cancel: bool = True) -> None:
        super().init(author_id=author_id, candidates=candidates, title=title, prompt=prompt, timeout=timeout, page_size=page_size, allow_cancel=allow_cancel, confirm_label="تثبيت الصوت العادي", cancel_label="إلغاء")
        self.force_button = discord.ui.Button(label="👑 حُكم الإعدام الملكي الفوري", style=discord.ButtonStyle.danger)
        self.force_button.callback = self._on_force
        self.add_item(self.force_button)

    async def _on_force(self, interaction: discord.Interaction) -> None:
        if self.selected_id is None:
            await interaction.response.send_message("اختر الهدف أولاً ثم اضغط على زر الإعدام الملكي الصاعق.", ephemeral=True)
            return
        self.result = -self.selected_id  # نستخدم إشارة سالبة لتمييز خيار الإعدام الفوري الصادر من الملك
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=f"{self._render_content()}\n\n⚖️ تم استدعاء مرسوم الملك الصارم وصعق الهدف.", view=self)
        self.stop()

> Yasser:
# ==============================================================================
# 5. محرك إدارة الأطوار الرئيسي والعمليات الحسابية والمنطقية (Werewolf Phase Cog)
# ==============================================================================

class WerewolfPhaseCog(commands.Cog):
    def init(self, bot: commands.Bot):
        self.bot = bot

    def _alive_ids(self, game: GameInstance) -> List[int]:
        return [int(uid) for uid in game.alive_players if uid in game.players and game.players[uid]['alive']]

    def _player_label(self, game: GameInstance, user_id: int) -> str:
        p = game.players.get(user_id)
        if not p:
            return f"لاعب #{user_id}"
        return p.get('display_name') or f"لاعب #{user_id}"

    def _candidate_list(self, game: GameInstance, *, exclude_ids: set[int] = None, allow_wolves: bool = True, allow_self: bool = True, requester_id: Optional[int] = None) -> List[Tuple[int, str]]:
        exclude_ids = exclude_ids or set()
        candidates: List[Tuple[int, str]] = []
        for uid in self._alive_ids(game):
            if uid in exclude_ids:
                continue
            if not allow_self and requester_id is not None and uid == requester_id:
                continue
            p = game.players.get(uid)
            if not p:
                continue
            if not allow_wolves and normalize_role(p.get('role')) == "الذيب":
                continue
            candidates.append((uid, self._player_label(game, uid)))
        return candidates

    def is_game_over(self, game: GameInstance) -> Tuple[bool, Optional[str]]:
        alive_uids = self._alive_ids(game)
        wolves = sum(1 for uid in alive_uids if normalize_role(game.players[uid]['role']) == "الذيب")
        others = len(alive_uids) - wolves
        if wolves == 0:
            return True, "villagers"
        if wolves >= others:
            return True, "wolves"
        return False, None

    async def run_night_phase(self, game: GameInstance, guild: discord.Guild, town_channel: discord.abc.Messageable, timeout: int = 75) -> NightTargets:
        game.game_phase = "night"
        await db.save_game_state(game.guild_id, "night", game.players, game.day_number)
        
        await town_channel.send(embed=embed_night_announcement())

        alive_snapshots = [(uid, game.players[uid]) for uid in self._alive_ids(game)]
        wolves = [uid for uid, p in alive_snapshots if normalize_role(p['role']) == "الذيب"]
        detective_ids = [uid for uid, p in alive_snapshots if normalize_role(p['role']) == "المحقق"]
        guard_ids = [uid for uid, p in alive_snapshots if normalize_role(p['role']) == "الحارس"]
        doctor_ids = [uid for uid, p in alive_snapshots if normalize_role(p['role']) == "الطبيب"]
        seducer_ids = [uid for uid, p in alive_snapshots if normalize_role(p['role']) == "المغرية"]

        targets = NightTargets()

        # إرسال طلبات اتخاذ القرارات بالخاص للاعبين ذوي الأدوار الحيوية
        wolf_tasks = []
        for uid in wolves:
            member = guild.get_member(uid) or await guild.fetch_member(uid)
            if not member: continue
            cands = self._candidate_list(game, allow_wolves=False, allow_self=False, requester_id=uid)
            view = BasePagedTargetView(author_id=uid, candidates=cands, title="ليلة الذئاب الدموية 🐺", prompt="اختر فريستك لهذا الليل لتنهشوا لحمه، ثم ثبت القرار.")
            embed = discord.Embed(title=view.title, description=view.prompt, color=0x8B0000)
            try:
                msg = await member.send(embed=embed, view=view)
                view.message = msg
                wolf_tasks.append(view)
            except discord.Forbidden:
                await town_channel.send(f"⚠️ {member.display_name} (الذيب) الخاص عندك مغلق! افتحه لتشارك بالصيد!")

> Yasser:
det_tasks = []
        for uid in detective_ids:
            if game.players[uid].get('has_used_power', False): continue
            member = guild.get_member(uid) or await guild.fetch_member(uid)
            if not member: continue
            cands = self._candidate_list(game, allow_self=False, requester_id=uid)
            view = BasePagedTargetView(author_id=uid, candidates=cands, title="ليلة المحقق كونان 🔍", prompt="اختر لاعباً لكشف هويته الكاملة. (متاح مرة واحدة فقط بالقيم).")
            embed = discord.Embed(title=view.title, description=view.prompt, color=0x3498db)
            try:
                msg = await member.send(embed=embed, view=view)
                view.message = msg
                det_tasks.append(view)
            except discord.Forbidden:
                await town_channel.send(f"⚠️ المحقق {member.display_name} الخاص عندك مغلق!")

        guard_tasks = []
        for uid in guard_ids:
            if game.players[uid].get('has_used_power', False): continue
            member = guild.get_member(uid) or await guild.fetch_member(uid)
            if not member: continue
            cands = self._candidate_list(game, allow_self=True, requester_id=uid)
            view = BasePagedTargetView(author_id=uid, candidates=cands, title="ليلة الحارس اليقظ 🛡️", prompt="اختر لاعباً لتعطيه درع الحماية ضد القتل (مرة واحدة فقط).")
            embed = discord.Embed(title=view.title, description=view.prompt, color=0x27ae60)
            try:
                msg = await member.send(embed=embed, view=view)
                view.message = msg
                guard_tasks.append(view)
            except discord.Forbidden:
                await town_channel.send(f"⚠️ الحارس {member.display_name} الخاص عندك مغلق!")

        doc_tasks = []
        for uid in doctor_ids:
            member = guild.get_member(uid) or await guild.fetch_member(uid)
            if not member: continue
            last_target = game.players[uid]['night_flags'].get('doctor_last_target', None)
            cands = self._candidate_list(game, allow_self=True, requester_id=uid, exclude_ids={last_target} if last_target else set())
            view = BasePagedTargetView(author_id=uid, candidates=cands, title="ليلة الطبيب المسعف ⚕️", prompt="اختر من تريد حمايته من الموت الليلة (لا يمكنك حماية نفس الشخص متتالياً).")
            embed = discord.Embed(title=view.title, description=view.prompt, color=0xe74c3c)
            try:
                msg = await member.send(embed=embed, view=view)
                view.message = msg
                doc_tasks.append(view)
            except discord.Forbidden:
                await town_channel.send(f"⚠️ الطبيب {member.display_name} الخاص عندك مغلق!")

        sed_tasks = []
        for uid in seducer_ids:
            member = guild.get_member(uid) or await guild.fetch_member(uid)
            if not member: continue
            cands = self._candidate_list(game, allow_self=False, requester_id=uid)
            view = BasePagedTargetView(author_id=uid, candidates=cands, title="ليلة المغرية الفاتنة 💃", prompt="اختر من ستزورينه؛ لو كان ذيباً تموتين معه، وإن كان مدنيًا مستهدفاً تحمينه.")
            embed = discord.Embed(title=view.title, description=view.prompt, color=0x9b59b6)
            try:
                msg = await member.send(embed=embed, view=view)
                view.message = msg
                sed_tasks.append(view)
            except discord.Forbidden:
                await town_channel.send(f"⚠️ المغرية {member.display_name} الخاص عندك مغلق!")

        # انتظار انتهاء فترات اتخاذ القرار لكل لاعب
        await asyncio.sleep(timeout)

        # استخلاص النتائج الجاهزة
        wolf_votes = [v.result for v in wolf_tasks if v.result is not None]
        if wolf_votes:
            counts = Counter(wolf_votes)
            top_score = max(counts.values())
            top_targets = [uid for uid, count in counts.items() if count == top_score]
            targets.wolves_target = random.choice(top_targets)

> Yasser:
targets.detective_target = next((v.result for v in det_tasks if v.result is not None), None)
        targets.guard_target = next((v.result for v in guard_tasks if v.result is not None), None)
        targets.doctor_target = next((v.result for v in doc_tasks if v.result is not None), None)
        targets.seducer_target = next((v.result for v in sed_tasks if v.result is not None), None)

        # تطبيق الحركات والقوى وتحديث قواعد البيانات
        if targets.detective_target is not None and detective_ids:
            det_uid = detective_ids[0]
            game.players[det_uid]['has_used_power'] = True
            await db.update_player_status(game.guild_id, det_uid, has_used_power=True)
            
            det_member = guild.get_member(det_uid)
            inspected_player = game.players.get(targets.detective_target)
            if det_member and inspected_player:
                role_info = inspected_player.get('role', 'قروي')
                try:
                    await det_member.send(f"🔍 نتيجة بحثك السري: اللاعب {inspected_player['display_name']} دوره الحقيقي هو: {role_info}.")
                except discord.Forbidden:
                    pass

        if targets.guard_target is not None and guard_ids:
            guard_uid = guard_ids[0]
            game.players[guard_uid]['has_used_power'] = True
            await db.update_player_status(game.guild_id, guard_uid, has_used_power=True)
            targets.protected_targets.add(targets.guard_target)

        if targets.doctor_target is not None and doctor_ids:
            doc_uid = doctor_ids[0]
            game.players[doc_uid]['night_flags']['doctor_last_target'] = targets.doctor_target
            targets.protected_targets.add(targets.doctor_target)

        # معالجة منطق المغرية المعقد
        seducer_uid = seducer_ids[0] if seducer_ids else None
        if targets.seducer_target is not None and seducer_uid is not None:
            t_player = game.players.get(targets.seducer_target)
            if t_player and normalize_role(t_player['role']) == "الذيب":
                targets.seducer_died_with_wolf = True
                targets.deaths[seducer_uid] = "المغرية زارت ذئباً فابتلعها ليل الغدر."
                targets.deaths[targets.seducer_target] = "المغرية التفت حول الذئب فهلك معها في الظلال."
            else:
                if targets.wolves_target is not None and targets.seducer_target == targets.wolves_target:
                    targets.protected_targets.add(targets.seducer_target)

        # معالجة هجوم الذئاب الأساسي
        if targets.wolves_target is not None:
            if targets.wolves_target not in targets.protected_targets:
                targets.deaths[targets.wolves_target] = "قُتل على يد مخالب الذئاب الشرسة."

        # تنفيذ حالات الوفاة في القناة الرسمية وتطبيق ميزة أم فادي الفاضحة
        someone_died = False
        for dead_uid, reason in list(targets.deaths.items()):
            p_record = game.players.get(dead_uid)
            if not p_record or not p_record['alive']: continue

            p_record['alive'] = False
            if dead_uid in game.alive_players:
                game.alive_players.remove(dead_uid)
            someone_died = True

            await db.update_player_status(game.guild_id, dead_uid, is_alive=False)
            await town_channel.send(embed=embed_morning_death(p_record['display_name'], p_record['role']))

            # إذا قُتلت أم فادي بواسطة الذئاب تفضح أحدهم فوراً
            if normalize_role(p_record['role']) == "أم فادي" and "الذئاب" in reason:
                active_wolves = [uid for uid in self._alive_ids(game) if normalize_role(game.players[uid]['role']) == "الذيب"]
                if active_wolves:
                    exposed_wolf = random.choice(active_wolves)
                    w_record = game.players[exposed_wolf]

> Yasser:
embed_fadi = discord.Embed(
                        title="👵 صرخة أم فادي الفاضحة من القبر!",
                        description=f"قبل أن تلفظ أنفاسها الأخيرة، التفتت للجميع وأشارت بإصبعها:\n\nالذيب هو {w_record['display_name']} يا قرويين خذوا ثأري!",
                        color=0xf1c40f
                    )
                    await town_channel.send(embed=embed_fadi)

        if not someone_died:
            await town_channel.send(embed=embed_morning_no_death())

        return targets

    async def run_day_voting(self, game: GameInstance, guild: discord.Guild, town_channel: discord.abc.Messageable, timeout_seconds: int = 120) -> Optional[int]:
        game.game_phase = "day"
        await db.save_game_state(game.guild_id, "day", game.players, game.day_number)

        alive_snapshots = [(uid, game.players[uid]) for uid in self._alive_ids(game)]
        alive_ids = {uid for uid, _ in alive_snapshots}

        session = VoteSession(eligible_voters=alive_ids)

        async def send_vote_view_to_member(uid: int, player_data: Dict):
            member = guild.get_member(uid) or await guild.fetch_member(uid)
            if not member: return
            cands = self._candidate_list(game, allow_self=False, requester_id=uid)
            if not cands: return

            is_king = normalize_role(player_data['role']) == "الملك"
            has_used_king = player_data.get('has_used_power', False)

            if is_king and not has_used_king:
                view = KingForceVoteView(author_id=uid, candidates=cands, title="تصويت النهار السيادي 👑", prompt="اختر خيارك؛ يمكنك التصويت بشكل عادي أو الضغط على زر الإعدام الملكي الفوري لإيقاف المحكمة وطرد الشخص مباشرة!")
            else:
                view = BasePagedTargetView(author_id=uid, candidates=cands, title="تصويت النهار للقرية 🏛️", prompt="اختر من تظن أنه الذئب الخفي لتعليقه على المشنقة.")

            embed = discord.Embed(title=view.title, description=view.prompt, color=0xe67e22)
            embed.set_footer(text="تصويتك سري بالكامل ولا يظهر للعلن.")
            try:
                msg = await member.send(embed=embed, view=view)
                view.message = msg
            except discord.Forbidden:
                return

            await view.wait()
            if view.result is None:
                return

            # إذا تم استخدام قوة الملك الفورية (القيمة سالبة)
            if view.result < 0:
                actual_target = abs(view.result)
                did_force = await session.force_lynch(actual_target)
                if did_force:
                    game.players[uid]['has_used_power'] = True
                    await db.update_player_status(game.guild_id, uid, has_used_power=True)
                    
                    target_player = game.players.get(actual_target)
                    embed_king = discord.Embed(
                        title="⚖️ مرسوم ملكي عاجل وقاطع!",
                        description=f"استدعى الملك سلطته المطلقة! أمر بإعدام {target_player['display_name']} فوراً وبدون أي نقاش أو تصويت إضافي!",
                        color=0x9b59b6
                    )
                    await town_channel.send(embed=embed_king)
                return

            await session.record_vote(uid, view.result)

        for uid, p_data in alive_snapshots:
            asyncio.create_task(send_vote_view_to_member(uid, p_data))

        try:
            await asyncio.wait_for(session.finished.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            pass

        # حسم الإعدام الفوري الخاص بالملك
        if session.force_used and session.force_target is not None:
            lynch_target = session.force_target
            t_player = game.players.get(lynch_target)
            if t_player and t_player['alive']:
                t_player['alive'] = False
                if lynch_target in game.alive_players:
                    game.alive_players.remove(lynch_target)
                await db.update_player_status(game.guild_id, lynch_target, is_alive=False)
            return lynch_target

> Yasser:
# فرز وحساب الأصوات العادية مع احتساب الوزن المضاعف للعمدة
        if not session.ballots:
            embed_no_votes = discord.Embed(title="🏛️ صمت النهار المحير", description="لم يقم أحد بالتصويت أو الاستجابة، انقضى النهار بسلام غامض.", color=0x7f8c8d)
            await town_channel.send(embed=embed_no_votes)
            return None

        tally: Counter[int] = Counter()
        for voter_id, target_id in session.ballots.items():
            voter = game.players.get(voter_id)
            if not voter or not voter['alive']: continue
            weight = 2 if normalize_role(voter['role']) == "العمدة" else 1
            tally[target_id] += weight

        if not tally:
            return None

        top_score = max(tally.values())
        top_targets = [uid for uid, score in tally.items() if score == top_score]
        lynch_target = random.choice(top_targets)

        target_player = game.players.get(lynch_target)
        if target_player and target_player['alive']:
            target_player['alive'] = False
            if lynch_target in game.alive_players:
                game.alive_players.remove(lynch_target)
            await db.update_player_status(game.guild_id, lynch_target, is_alive=False)

        # عرض تفاصيل ونتائج فرز الأصوات للعامة
        vote_lines = []
        for uid, score in tally.most_common():
            p = game.players.get(uid)
            if not p: continue
            vote_lines.append(f"• {p['display_name']} — حصل على {score} أصوات")

        embed_res = discord.Embed(
            title="⚖️ حكم المشنقة النهائي للقرية",
            description="انتهت مداولات النهار وحضر الجلاد.\n\n" + "\n".join(vote_lines) + f"\n\nالنتيجة: تم اقتياد اللاعب {target_player['display_name']} للمشنقة بعد نيله أعلى الأصوات وطُرِد من اللعبة.",
            color=0xd35400
        )
        await town_channel.send(embed=embed_res)
        return lynch_target

# ==============================================================================
# 6. البوت الرئيسي والأوامر التفاعلية والربط (Main Bot & Slash Commands)
# ==============================================================================

class WerewolfBot(commands.Bot):
    def init(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().init(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await db.initialize()
        await self.add_cog(WerewolfPhaseCog(self))
        await self.tree.sync()
        print("— تم مزامنة الأوامر وربط محرك الأطوار المتقدمة بنجاح —")

bot = WerewolfBot()

@bot.event
async def on_ready():
    print(f"😎 المدير جاهز للغدر والطقطقة الحية! البوت يعمل الآن باسم: {bot.user.name}")

# السير الموجه والآلي لكامل اللعبة بشكل متتابع ومتكامل
async def manage_game_loop(guild_id: int, interaction: discord.Interaction):
    game = games.get(guild_id)
    cog: Optional[WerewolfPhaseCog] = bot.get_cog("WerewolfPhaseCog")
    if not game or not cog: return

    town_channel = interaction.channel

    while game.game_started:
        game.day_number += 1
        
        # 1. تشغيل طور الليل وحصد الحركات
        await cog.run_night_phase(game, interaction.guild, town_channel, timeout=40)
        
        # فحص شروط انتهاء اللعبة فوراً بعد انتهاء أحداث الليل السري
        over, winner = cog.is_game_over(game)
        if over:
            if winner == "wolves":
                await town_channel.send(embed=embed_wolves_victory())
            else:
                await town_channel.send(embed=embed_villagers_victory())
            game.game_started = False
            game.game_phase = "ended"
            await db.save_game_state(guild_id, "ended", game.players, game.day_number)
            break

        # 2. تشغيل طور النهار والتصويت الخاص
        await town_channel.send(f"🏛️ بدأ نهار اليوم الـ {game.day_number}! تم فتح صناديق الاقتراع السرية بالخاص، لديكم دقيقة واحدة للنقاش والتصويت!")
        await cog.run_day_voting(game, interaction.guild, town_channel, timeout_seconds=40)

> Yasser:
# فحص شروط انتهاء اللعبة فوراً بعد فرز أصوات المشنقة بالنهار
        over, winner = cog.is_game_over(game)
        if over:
            if winner == "wolves":
                await town_channel.send(embed=embed_wolves_victory())
            else:
                await town_channel.send(embed=embed_villagers_victory())
            game.game_started = False
            game.game_phase = "ended"
            await db.save_game_state(guild_id, "ended", game.players, game.day_number)
            break

# ==================== أمر بدء التسجيل لإنشاء قيم جديد ====================
@bot.tree.command(name="تسجيل", description="فتح التسجيل وانضمام اللاعبين للقيم الجديد")
async def register_game(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    
    # محاولة استرجاع حالة سابقة غير منتهية من قاعدة البيانات
    db_phase, db_day, db_players = await db.load_game_state(guild_id)
    if db_phase and db_phase != 'ended' and guild_id not in games:
        games[guild_id] = GameInstance.from_state(guild_id, db_phase, db_day, db_players)
        games[guild_id].channel_id = interaction.channel_id

    if guild_id in games and games[guild_id].game_started:
        await interaction.response.send_message("❌ فيه قيم شغال ومعمعة حامية حالياً يا كابتن! خلّصوه أول.", ephemeral=True)
        return

    games[guild_id] = GameInstance(guild_id)
    games[guild_id].channel_id = interaction.channel_id
    await db.save_game_state(guild_id, "signup", {}, 0)

    embed = discord.Embed(
        title="🐺 لـعـبـة الـذ يـب الـمـطـوّرة 🐺",
        description="يا هلا بالشباب! تم فتح باب التسجيل الإلكتروني للقيم الجديد والمطور.\nاضغط على الزر بالأسفل لتسجيل اسمك بالمعمعة. خلك جاهز للغدر المخطط له! 😂🏃‍♂️",
        color=0x2c3e50
    )
    embed.set_footer(text="ملاحظة هامة: تحتاجون 4 لاعبين كحد أدنى عشان تبدأ المتعة الإستراتيجية صح.")

    view = discord.ui.View(timeout=None)
    join_btn = discord.ui.Button(label="انضمام للقيم الحالي 🚪", style=discord.ButtonStyle.blurple)

    async def join_callback(inter: discord.Interaction):
        game = games[guild_id]
        if inter.user.id in game.players:
            await inter.response.send_message("أنت مسجل وداخل ومسنتر باللعبة من أول، اهدأ واصبر! 🤦‍♂️", ephemeral=True)
            return

        game.players[inter.user.id] = {
            'role': 'القروي',
            'alive': True,
            'has_used_power': False,
            'display_name': inter.user.display_name,
            'night_flags': {}
        }
        if inter.user.id not in game.alive_players:
            game.alive_players.append(inter.user.id)

        await db.save_game_state(guild_id, "signup", game.players, game.day_number)
        await inter.response.send_message(f"✅ كفو! انضم البطل {inter.user.display_name} للقيم. (العدد الحالي للجنود: {len(game.players)})")

    join_btn.callback = join_callback
    view.add_item(join_btn)
    await interaction.response.send_message(embed=embed, view=view)

# ==================== أمر توزيع الأدوار وبدء الأطوار ====================
@bot.tree.command(name="ابدأ_اللعبه", description="توزيع الأدوار الإستراتيجية السرية وبدء أول ليل")
async def start_game(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    
    if guild_id not in games:
        await interaction.response.send_message("❌ اكتب أمر /تسجيل أولاً لتأسيس قيم جديد في هذا السيرفر!", ephemeral=True)
        return

    game = games[guild_id]
    if len(game.players) < 4:
        await interaction.response.send_message(f"❌ وين رايح بدون لاعبين؟ العدد الحالي {len(game.players)}! لازم 4 لاعبين على الأقل عشان تبدأ الطقطقة المتكاملة!", ephemeral=True)
        return

    if game.game_started:
        await interaction.response.send_message("القيم في أوج اشتعاله وأطواره تدور بالفعل، ركز معنا!", ephemeral=True)
        return

    player_ids = list(game.players.keys())
    random.shuffle(player_ids)

> Yasser:
# قائمة الأدوار الخاصة المدعومة
    available_roles = list(ROLES_INFO.keys())
    if "القروي" in available_roles:
        available_roles.remove("القروي")
    if "الذيب" in available_roles:
        available_roles.remove("الذيب")

    # ضمان وجود ذئب خفي واحد على الأقل بالمجموعة
    game.players[player_ids[0]]['role'] = "الذيب"
    
    # توزيع بقية الأدوار الخاصة عشوائياً، وما يفيض يتم إسناده كقروي عادي
    for i in range(1, len(player_ids)):
        if i - 1 < len(available_roles):
            game.players[player_ids[i]]['role'] = available_roles[i - 1]
        else:
            game.players[player_ids[i]]['role'] = "القروي"

    # إرسال بطاقات الأدوار السرية بالكامل عبر الرسائل الخاصة للاعبين بشكل آمن
    for p_id in player_ids:
        member = interaction.guild.get_member(p_id) or await bot.fetch_user(p_id)
        if member:
            role_assigned = game.players[p_id]['role']
            role_description = ROLES_INFO[role_assigned]
            try:
                await member.send(f"🤫 بطاقتك ودورك السري في هذا القيم هو:\n\n[{role_assigned}]\n{role_description}")
            except discord.Forbidden:
                await interaction.channel.send(f"⚠️ يا {member.display_name} افتح الخاص عندك لتتمكن من رؤية دورك السري، تم تخزين دورك بالقاعدة على أي حال!")

    game.game_started = True
    game.game_phase = "night"
    await db.save_game_state(guild_id, "night", game.players, game.day_number)

    await interaction.response.send_message("⚔️ تم قفل التسجيل وتوزيع البطاقات بالخاص بنجاح! جاري تشغيل المحرك التلقائي للأطوار...")
    
    # تشغيل الحلقة اللانهائية الآلية لإدارة الأطوار والتعاقب
    asyncio.create_task(manage_game_loop(guild_id, interaction))

# ==================== أمر المحقق اليدوي التوافقي ====================
@bot.tree.command(name="تحقيق", description="[المحقق] كشف هوية لاعب مستهدف في غضون القيم (مرة واحدة)")
async def investigate(interaction: discord.Interaction, target: discord.User):
    guild_id = interaction.guild_id
    if guild_id not in games or not games[guild_id].game_started:
        await interaction.response.send_message("العب غيرها يا كابتن، اللعبة ما بدأت أصلاً!", ephemeral=True)
        return

    game = games[guild_id]
    voter_id = interaction.user.id

    if voter_id not in game.players:
        await fire_not_in_game(interaction)
        return

    if normalize_role(game.players[voter_id]['role']) != "المحقق":
        await interaction.response.send_message("❌ مسوي فيها كونان وذكي؟ أنت لست المحقق بهذا القيم، العب كقروي وأنت ساكت!", ephemeral=True)
        return

    if game.players[voter_id].get('has_used_power', False):
        await fire_already_used_power(interaction, "المحقق")
        return

    if target.id not in game.players:
        await interaction.response.send_message("هذا الشخص المستهدف خارج نطاق اللعبة الحالية، ركز!", ephemeral=True)
        return

    # كشف الهوية فورياً وحفظ الحالة
    target_role = game.players[target.id]['role']
    game.players[voter_id]['has_used_power'] = True
    await db.update_player_status(guild_id, voter_id, has_used_power=True)

    await interaction.response.send_message(f"🔍 بنتيجة بحثك الإداري السريع: اللاعب {target.display_name} دوره الفعلي هو: {target_role} 🤫 (لا تفضح نفسك وعلم السيرفر بالتصويت ملمحاً!)", ephemeral=True)

# ==================== أمر فحص لوحة الحالة الحالية ====================
@bot.tree.command(name="الحالة", description="استعراض أسماء الأحياء الباقين وعدد الضحايا في السيرفر")
async def game_status(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    
    # محاولة جلب البيانات في حال عدم وجودها بالذاكرة
    if guild_id not in games:
        db_phase, db_day, db_players = await db.load_game_state(guild_id)
        if db_phase and db_phase != 'ended':
            games[guild_id] = GameInstance.from_state(guild_id, db_phase, db_day, db_players)
        else:
            await interaction.response.send_message("ما فيه أي قيم شغال حالياً في هذا السيرفر. اكتب أمر /تسجيل واصنع متعتك!", ephemeral=True)
            return

> Yasser:
game = games[guild_id]
    alive_names = [game.players[uid]['display_name'] for uid in game.alive_players if uid in game.players and game.players[uid]['alive']]
    dead_count = len(game.players) - len(alive_names)

    embed = discord.Embed(title=f"📊 وضع لوحة التحكم للقيم الحالي (اليوم: {game.day_number})", color=0x3498db)
    embed.add_field(name="🟢 الأحياء الصامدين والواقفين حالياً:", value=", ".join(alive_names) if alive_names else "لا يوجد أحد (إبادة جماعية للقرية)", inline=False)
    embed.add_field(name="💀 عدد الضحايا الكلي الذين سقطوا:", value=str(dead_count), inline=False)
    embed.add_field(name="⚙️ المرحلة والطور الحالي للعبة:", value=f"{game.game_phase.upper()}", inline=True)
    embed.set_footer(text="البوت يراقب ويحفظ كل شيء بقاعدة البيانات للتوثيق.")

    await interaction.response.send_message(embed=embed)

# ==============================================================================
# 7. تشغيل البوت وإرفاق ملف التوكين بأمان (Fixing Event Loop)
# ==============================================================================
async def main():
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("❌ خطأ: لم يتم العثور على رمز التوكين السري DISCORD_TOKEN داخل ملف .env")
        return

    # تشغيل البوت باستخدام context manager لضمان الإغلاق السليم
    async with bot:
        try:
            await bot.start(token)
        except KeyboardInterrupt:
            pass
        finally:
            # إغلاق قاعدة البيانات بأمان قبل إغلاق الـ Loop كاملاً
            await db.close()
            print("💾 تم إغلاق قاعدة البيانات بنجاح.")

if name == "main":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("👋 تم إيقاف البوت يدويًا.")
