import os
import discord
from discord.ext import commands
import aiosqlite
import asyncio
import random
from typing import Dict, Tuple, Optional, List

# ==============================================================================
# 1. BOT SETUP & INTENTS
# ==============================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==============================================================================
# 2. ASYNCHRONOUS DATABASE FOR TRADITIONAL REWARDS
# ==============================================================================
class DatabaseManager:
    def __init__(self, db_path: str = "vale_traditional_werewolf.db"):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                points INTEGER DEFAULT 0
            )
        """)
        await self.conn.commit()

    async def get_points(self, user_id: int) -> int:
        async with self.conn.execute("SELECT points FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def add_points(self, user_id: int, points: int) -> int:
        current = await self.get_points(user_id)
        new_points = max(0, current + points)
        await self.conn.execute("""
            INSERT OR REPLACE INTO users (user_id, points) VALUES (?, ?)
        """, (user_id, new_points))
        await self.conn.commit()
        return new_points

    async def close(self):
        if self.conn:
            await self.conn.close()

db = DatabaseManager()

# ==============================================================================
# 3. GLOBAL TRADITIONAL ENGINE STATE
# ==============================================================================
active_games: Dict[int, Dict] = {}

def get_game_state(guild_id: int) -> Dict:
    if guild_id not in active_games:
        active_games[guild_id] = {
            "status": "LOBBY",
            "host_id": None,
            "players": {},
            "votes": {},
            "night_actions": {"kill": None, "save": None, "check": None, "shield": None, "visit": None},
            "day_count": 1,
            "king_execution": None,
            "umzaki_expose_id": None
        }
    return active_games[guild_id]

# ==============================================================================
# 4. TRADITIONAL SAUDI JOKES & COMMENTS
# ==============================================================================
WOLF_WIN_COMMENTS = [
    "الذيابة عاثوا في الأرض فساداً ونفضوكم نفض! كفو يا ذيابة السيرفر 😎🐺",
    "اشهد انكم ذيابة شقيتوا القرويين شق وطيرتوا هيبتهم كلياً 🔥",
    "تمت إبادة القرية بنجاح وصارت مسلخاً للذيابة، جيبوا لهم كيس نقاط يستاهلون!"
]

VILLAGE_WIN_COMMENTS = [
    "القرويين الأبطال صكوا الذيابة لين قالوا بس! تنظفت الروم رسميّاً 🧑‍🌾",
    "فوز ساحق للمواطنين والشخصيات الطيبة والبركة في الذكاء والتحريات الأسطورية 👑",
    "تم دعس الذيابة ورميهم برا السيرفر، ناموا يا أهل Vale بأمان وعافية!"
]

DEATH_COMMENTS = [
    "ههههههههههههه معليش طرت بدري، رح سو لك شاهي ونور دكة الاحتياط 🏃‍♂️💨",
    "يا حليلك قمطوك الذيابة بالليل وطلعت برا السالفة تماماً هههههه تعيش وتاكل غيرها 💀",
    "ودع الملاعب رسميّاً! انقمط وقعد يطالع فينا من برا التشات، شاو يا حلو 🚶‍♂️"
]

MILESTONE_COMMENTS = [
    "اوووووه صك الـ 500 نقطة؟ من وين لك هذا يا لئيم؟! اعترف من هكرت؟ 🧐😂",
    "ما شاء الله 500 نقطة كاملة! صرت الهامور والتاجر الرسمي لـ Vale Community 💰"
]

def get_saudi_joke(joke_list: List[str]) -> str:
    return random.choice(joke_list)

# ==============================================================================
# 5. PERSISTENT INTERACTIVE UI (LOBBY & EXPLANATION)
# ==============================================================================
class TraditionalLobbyView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="انضمام للعبة 🐺", style=discord.style.green, custom_id="join_traditional_werewolf")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = get_game_state(interaction.guild_id)
        if game["status"] != "LOBBY":
            await interaction.response.send_message("❌ اللعبة بدأت خلاص، راحت عليك الجولة!", ephemeral=True)
            return
        if interaction.user.id in game["players"]:
            await interaction.response.send_message("أنت مسجل في القائمة ومنتظر بالفعل بزيادة حماس!", ephemeral=True)
            return

        game["players"][interaction.user.id] = {
            "role": "Citizen",
            "alive": True,
            "has_used_power": False,
            "name": interaction.user.display_name
        }
        
        embed = interaction.message.embeds[0]
        p_mentions = [f"• <@{pid}>" for pid in game["players"].keys()]
        embed.description = f"**المضيف:** <@{game['host_id']}>\n\n**قائمة المشتركين الحالية ({len(game['players'])}):**\n" + "\n".join(p_mentions)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="انسحاب 🏃‍♂️", style=discord.style.danger, custom_id="leave_traditional_werewolf")
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = get_game_state(interaction.guild_id)
        if game["status"] != "LOBBY":
            await interaction.response.send_message("ما يمديك تنسحب، اللعبة بدأت والغدر شغال الحين!", ephemeral=True)
            return
        if interaction.user.id not in game["players"]:
            await interaction.response.send_message("أنت مو مشترك أصلاً عشان تنسحب هههههه!", ephemeral=True)
            return

        del game["players"][interaction.user.id]
        embed = interaction.message.embeds[0]
        p_mentions = [f"• <@{pid}>" for pid in game["players"].keys()]
        embed.description = f"**المضيف:** <@{game['host_id']}>\n\n**قائمة المشتركين الحالية ({len(game['players'])}):**\n" + "\n".join(p_mentions)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="شرح شخصيات اللعبة 📚", style=discord.style.blurple, custom_id="help_traditional_werewolf")
    async def help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        help_embed = discord.Embed(title="📚 كتيب قوانين وشخصيات الذئب التقليدية الكلاسيكية", color=discord.Color.gold())
        help_embed.add_field(name="🐺 الذيب", value="يحاول التخلص من جميع الشخصيات والسيطرة على اللعبة بالكامل.", inline=False)
        help_embed.add_field(name="🧑‍🌾 القروي", value="شخصية عادية، ما عنده قدرة خاصة لكن يشارك بالتصويت ويكشف الذيابة بالذكاء والتحليل.", inline=False)
        help_embed.add_field(name="🔍 المحقق", value="يقدر يكشف هوية أي لاعب **مرة واحدة فقط** طوال القيم على العام برسالة مخفية تظهر له وحده.", inline=False)
        help_embed.add_field(name="🛡️ الحارس", value="يعطي درع حماية لأي لاعب ويحميه من القتل **مرة واحدة فقط** بالقيم.", inline=False)
        help_embed.add_field(name="👑 الملك", value="يملك سلطة تحويل جميع الأصوات على لاعب واحد وطرده مباشرة **مرة واحدة فقط** بالقيم عبر زر خاص نهاراً.", inline=False)
        help_embed.add_field(name="🏛️ العمدة", value="صوته أقوى من الجميع، حيث يُحسب التصويت الخاص فيه بـ **صوتين** تلقائياً في الفرز.", inline=False)
        help_embed.add_field(name="⚕️ الطبيب", value="يستطيع حماية أي لاعب من القتل طوال القيم، لكن لازم يختار بحذر كل ليلة.", inline=False)
        help_embed.add_field(name="💃 المغرية", value="إذا زارت شخص وكان ذيب تموت معه. أما إذا كان شخص عادي وهجمت عليه الذيابة، فإنها تحميه من القتل.", inline=False)
        help_embed.add_field(name="👵 أم زكي", value="إذا قتلتها الذيابة بالليل، تقوم بفضح أحد الذيابة عشوائياً قبل موتها بالصباح.", inline=False)
        await interaction.response.send_message(embed=help_embed, ephemeral=True)

# ==============================================================================
# 6. IN-CHANNEL REVEAL & NIGHT ACTION SELECTION COMPONENTS (EPHEMERAL)
# ==============================================================================
class RevealRoleView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="اكشف هويتك السرية 🕵️‍♂️", style=discord.style.blurple, custom_id="reveal_my_secret_role")
    async def reveal_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = active_games.get(self.guild_id)
        if not game or game["status"] == "LOBBY":
            await interaction.response.send_message("الجيم ما بدأ حالياً عشان تكشف هويتك!", ephemeral=True)
            return
        if interaction.user.id not in game["players"]:
            await interaction.response.send_message("أنت لست من اللاعبين المشاركين في هذه الجولة!", ephemeral=True)
            return

        p_data = game["players"][interaction.user.id]
        role = p_data["role"]
        
        role_desc = {
            "Wolf": "Wolf 🐺 | **هويتك:** أنت **الذيب**!\nهدفك التخلص من جميع الشخصيات والسيطرة على اللعبة بالكامل بالتنسيق مع بقية الذيابة.",
            "Citizen": "Citizen 🧑‍🌾 | **هويتك:** أنت **قروي صالح**!\nما عندك قدرة خاصة لكن تشارك بالنهار وتكشف الذيابة بذكائك وتحليلك.",
            "Seer": "Detective 🔍 | **هويتك:** أنت **المحقق**!\nتقدر تكشف هوية وأي دور لاعب **مرة واحدة فقط طوال القيم** بالليل عبر الخيارات التفاعلية.",
            "Guardian": "Guardian 🛡️ | **هويتك:** أنت **الحارس**!\nتقدر تعطي درع حماية لأي لاعب وتحميه من القتل **مرة واحدة فقط بالقيم** بالليل.",
            "King": "King 👑 | **هويتك:** أنت **الملك**!\nتملك سلطة تحويل جميع أصوات النهار على لاعب واحد وطرده فوراً **مرة واحدة فقط بالقيم** عبر زر مخصص.",
            "Mayor": "Mayor 🏛️ | **هويتك:** أنت **العمدة**!\nصوتك أقوى من الجميع، حيث يُحسب التصويت الخاص فيك بـ **صوتين تلقائياً** في الفرز النهائي.",
            "Doctor": "Doctor ⚕️ | **هويتك:** أنت **الطبيب**!\nتستطيع حماية أي لاعب من القتل طوال القيم، تختار هدفك الطبي كل ليلة بعناية وحذر.",
            "Seductress": "Seductress 💃 | **هويتك:** أنتِ **المغرية**!\nإذا زرتِ شخص وكان ذيب تموتين معه فوراً، أما إذا زرتِ شخص عادي وهجمت عليه الذيابة تحمينه من الموت!",
            "UmZaki": "UmZaki 👵 | **هويتك:** أنتِ **أم زكي**!\nقدرة كامنة: إذا قتلتكِ الذيابة بالليل، تقومين بفضح اسم أحد الذيابة عشوائياً بعبارة دم في الصباح الكاشف!"
        }
        await interaction.response.send_message(role_desc.get(role, "خطأ في تحديد الهوية الحالية!"), ephemeral=True)


class TraditionalNightSelect(discord.ui.Select):
    def __init__(self, action_type: str, options_list: List[discord.SelectOption], guild_id: int):
        self.action_type = action_type
        self.guild_id = guild_id
        super().__init__(placeholder="اختر هدفك التكتيكي الليلة بعناية...", min_values=1, max_values=1, options=options_list)

    async def callback(self, interaction: discord.Interaction):
        game = active_games.get(self.guild_id)
        if not game or game["status"] != "NIGHT":
            await interaction.response.send_message("انتهى وقت الليل أو تم إلغاء الجيم حالياً!", ephemeral=True)
            return

        target_id = int(self.values[0])
        
        if self.action_type == "WOLF":
            game["night_actions"]["kill"] = target_id
            await interaction.response.send_message(f"🐺 تم تحديد الضحية لقتلها الليلة بنجاح!", ephemeral=True)
        elif self.action_type == "DOCTOR":
            game["night_actions"]["save"] = target_id
            await interaction.response.send_message(f"⚕️ اخترت توفير العلاج والحماية الطبية لهذا الشخص الليلة!", ephemeral=True)
        elif self.action_type == "GUARDIAN":
            game["night_actions"]["shield"] = target_id
            game["players"][interaction.user.id]["has_used_power"] = True
            await interaction.response.send_message(f"🛡️ قمت بتفعيل درع الحماية الخاص بك على الهدف (مرة واحدة بالقيم)!", ephemeral=True)
        elif self.action_type == "SEER":
            game["players"][interaction.user.id]["has_used_power"] = True
            target_data = game["players"][target_id]
            role_map = {
                "Wolf": "ذيب 🐺", "Citizen": "قروي صالح 🧑‍🌾", "Seer": "المحقق 🔍",
                "Guardian": "الحارس 🛡️", "King": "الملك 👑", "Mayor": "العمدة 🏛️",
                "Doctor": "الطبيب ⚕️", "Seductress": "المغرية 💃", "UmZaki": "أم زكي 👵"
            }
            readable_role = role_map.get(target_data["role"], "غير معروف")
            await interaction.response.send_message(f"🔍 **تقرير المحقق السري:** اللاعب المختار هويته الحقيقية هي: **{readable_role}**!", ephemeral=True)
        elif self.action_type == "SEDUCTRESS":
            game["night_actions"]["visit"] = target_id
            await interaction.response.send_message(f"💃 قررتِ زيارة هذا اللاعب وإغوائه تكتيكياً الليلة!", ephemeral=True)


class TraditionalNightActionView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="تنفيذ التحركات الليلية 🌌", style=discord.style.secondary, custom_id="trigger_night_choice_button")
    async def night_click(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = active_games.get(self.guild_id)
        if not game or game["status"] != "NIGHT":
            await interaction.response.send_message("الليل غير نشط حالياً!", ephemeral=True)
            return
        if interaction.user.id not in game["players"] or not game["players"][interaction.user.id]["alive"]:
            await interaction.response.send_message("أنت لست في اللعبة أو ميت حالياً! انتظر الصباح.", ephemeral=True)
            return

        p_data = game["players"][interaction.user.id]
        role = p_data["role"]

        alive_players = [pid for pid, p in game["players"].items() if p["alive"]]
        options_list = [discord.SelectOption(label=game["players"][pid]["name"], value=str(pid)) for pid in alive_players]

        if role == "Citizen" or role == "King" or role == "Mayor" or role == "UmZaki":
            await interaction.response.send_message("أنت قروي صالح/أو لا تملك قدرة ليلية حالياً، انتظر الصباح لتدافع عن نفسك بالتصويت!", ephemeral=True)
            return

        if (role == "Seer" or role == "Guardian") and p_data["has_used_power"]:
            await interaction.response.send_message("لقد استخدمت قدرتك التي تتاح لمرة واحدة فقط مسبقاً طوال هذا القيم!", ephemeral=True)
            return

        # إظهار القائمة المنسدلة السرية حسب نوع القوة
        view = discord.ui.View(timeout=60.0)
        if role == "Wolf":
            view.add_item(TraditionalNightSelect("WOLF", options_list, self.guild_id))
            await interaction.response.send_message("🐺 **قوة الافتراس الكامنة:** اختر ضحيتك لقتلها الليلة:", view=view, ephemeral=True)
        elif role == "Doctor":
            view.add_item(TraditionalNightSelect("DOCTOR", options_list, self.guild_id))
            await interaction.response.send_message("⚕️ **حقيبة الرعاية الطبية:** اختر اللاعب لحمايته من الموت الليلة:", view=view, ephemeral=True)
        elif role == "Seer":
            view.add_item(TraditionalNightSelect("SEER", options_list, self.guild_id))
            await interaction.response.send_message("🔍 **تحقيقات سرية كاشفة:** اختر هدفك لتعرف دوره الحقيقي الحين:", view=view, ephemeral=True)
        elif role == "Guardian":
            view.add_item(TraditionalNightSelect("GUARDIAN", options_list, self.guild_id))
            await interaction.response.send_message("🛡️ **درع الحماية الحارس:** اختر اللاعب الذي تود حمايته بوقاية الدرع التامة الليلة:", view=view, ephemeral=True)
        elif role == "Seductress":
            view.add_item(TraditionalNightSelect("SEDUCTRESS", options_list, self.guild_id))
            await interaction.response.send_message("💃 **إغواء تكتيكي:** اختاري من تودين زيارته هذه الليلة للتمويه المتبادل:", view=view, ephemeral=True)

# ==============================================================================
# 7. DAY VOTING & TRADITIONAL KING OVERRIDE INTERACTION
# ==============================================================================
class TraditionalVotingSelect(discord.ui.Select):
    def __init__(self, options_list: List[discord.SelectOption], guild_id: int):
        self.guild_id = guild_id
        super().__init__(placeholder="اختر الشخص الذي تريد التصويت ضده لطردة...", min_values=1, max_values=1, options=options_list)

    async def callback(self, interaction: discord.Interaction):
        game = active_games.get(self.guild_id)
        if not game or game["status"] != "DAY_VOTING":
            await interaction.response.send_message("التصويت مغلق حالياً!", ephemeral=True)
            return
        if interaction.user.id not in game["players"] or not game["players"][interaction.user.id]["alive"]:
            await interaction.response.send_message("الموتى لا يصوتون! خلك متفرج واستمتع 😂💀", ephemeral=True)
            return

        target_id = int(self.values[0])
        game["votes"][interaction.user.id] = target_id
        await interaction.response.send_message(f"✅ تم تسجيل صوتك بنجاح في الصندوق العلني ضد الهدف!", ephemeral=True)


class TraditionalDayVotingView(discord.ui.View):
    def __init__(self, options_list: List[discord.SelectOption], guild_id: int):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.add_item(TraditionalVotingSelect(options_list, guild_id))

    @discord.ui.button(label="👑 سلطة الملك الإقصائية", style=discord.style.blurple, custom_id="traditional_king_power_btn")
    async def king_power_click(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = active_games.get(self.guild_id)
        if not game or game["status"] != "DAY_VOTING":
            await interaction.response.send_message("لا يمكنك استخدامها الحين!", ephemeral=True)
            return
            
        p_data = game["players"].get(interaction.user.id)
        if not p_data or p_data["role"] != "King" or not p_data["alive"]:
            await interaction.response.send_message("❌ هذا الزر مخصص فقط للملك الحيّ الحقيقي للبلاد! حرك يدك هههههه", ephemeral=True)
            return
            
        if p_data["has_used_power"]:
            await interaction.response.send_message("❌ يا جلالة الملك لقد استخدمت سلطتك الإقصائية المطلقة مرة واحدة مسبقاً طوال القيم!", ephemeral=True)
            return

        alive_players = [pid for pid, p in game["players"].items() if p["alive"]]
        king_options = [discord.SelectOption(label=game["players"][pid]["name"], value=str(pid)) for pid in alive_players]

        class TraditionalKingSelectView(discord.ui.View):
            def __init__(self, g_id, k_id):
                super().__init__(timeout=60.0)
                self.g_id = g_id
                self.k_id = k_id

            @discord.ui.select(placeholder="اختر من تريد طرده فوراً بالسلطة الملكية...", options=king_options)
            async def select_callback(self, interact: discord.Interaction, select: discord.ui.Select):
                g = active_games[self.g_id]
                target = int(select.values[0])
                g["king_execution"] = target
                g["players"][self.k_id]["has_used_power"] = True
                await interact.response.send_message(f"👑 أبشر يا ملكنا! تم إصدار الأمر الملكي السامي بطرد المستهدف فوراً!", ephemeral=True)

        await interaction.response.send_message("👑 اختر من القائمة أدناه الشخص الذي تريد تحويل كل الأصوات ضده وطرده حالاً:", view=TraditionalKingSelectView(self.guild_id, interaction.user.id), ephemeral=True)

# ==============================================================================
# 8. TRADITIONAL TEXT COMMANDS (POINTS & RESET)
# ==============================================================================
@bot.command(name="نقاطي")
async def check_my_score(ctx):
    points = await db.get_points(ctx.author.id)
    embed = discord.Embed(title=f"📊 رصيد انتصاراتك في مجتمع Vale", color=discord.Color.blue())
    embed.add_field(name="النقاط الكلية 💰", value=f"`{points}` نقطة فوز كلاسيكية", inline=False)
    embed.set_footer(text="العب أكثر وصك الـ 500 لتصبح الهامور المعتمد")
    await ctx.send(embed=embed)
    
    if points >= 500:
        await ctx.send(f"📢 {ctx.author.mention} {get_saudi_joke(MILESTONE_COMMENTS)}")

@bot.command(name="تصفير_الذيب")
async def emergency_reset_game(ctx):
    guild_id = ctx.guild.id
    if guild_id in active_games:
        del active_games[guild_id]
    await ctx.send("♻️ **تم تصفير روم الذئب الكلاسيكية بنجاح!** الساحة ممسوحة وجاهزة لاستقبال حجز جولة جديدة ونظيفة كلياً.")

# ==============================================================================
# 9. CLASSIC LOBBY CREATION & COMMAND GENERATION
# ==============================================================================
@bot.command(name="ذيب")
async def create_classic_lobby(ctx):
    guild_id = ctx.guild.id
    game = get_game_state(guild_id)
    
    if game["status"] != "LOBBY":
        await ctx.send("في قيم ذيب كلاسيكي شغال حالياً بالسيرفر، انتظر ينتهي أو اكتب أمر التصفير `!تصفير_الذيب`!")
        return

    game["host_id"] = ctx.author.id
    game["players"] = {
        ctx.author.id: {
            "role": "Citizen",
            "alive": True,
            "has_used_power": False,
            "name": ctx.author.display_name
        }
    }

    embed = discord.Embed(
        title="🐺 لعبة الذئب التقليدية الكلاسيكية - مجتمع Vale",
        description=f"**المضيف المعتمد:** {ctx.author.mention}\n\n**قائمة المشتركين الحالية (1):**\n• {ctx.author.mention}",
        color=discord.Color.dark_purple()
    )
    embed.set_image(url="https://images.unsplash.com/photo-1590424753042-35677df1461f?q=80&w=600")
    embed.set_footer(text="اضغط على الأزرار بالأسفل لإدارة اشتراكك أو قراءة الكتيب التوضيحي للشخصيات الـ 9")

    view = TraditionalLobbyView(guild_id)
    await ctx.send(embed=embed, view=view)

@bot.command(name="ابدأ_الذيب")
async def start_classic_game(ctx):
    guild_id = ctx.guild.id
    game = active_games.get(guild_id)
    
    if not game or game["status"] != "LOBBY":
        await ctx.send("لم يتم فتح باب التسجيل بعد! اكتب `!ذيب` لفتح الروم أولاً.")
        return
    if ctx.author.id != game["host_id"]:
        await ctx.send("المضيف الذي فتح الجيم هو الوحيد المخول ببدء اللعب!")
        return
        
    p_count = len(game["players"])
    if p_count < 4:
        await ctx.send("العدد غير كافٍ للعب الاحترافي! نحتاج على الأقل 4 لاعبين لتوزيع الشخصيات بالشكل المطلوب.")
        return

    game["status"] = "DISTRIBUTING"
    
    p_ids = list(game["players"].keys())
    random.shuffle(p_ids)
    
    # توزيع الـ 9 أدوار الكلاسيكية حسب التوافر
    assigned_roles = ["Wolf", "Doctor", "Seer", "Seductress", "UmZaki", "King", "Mayor", "Guardian"]
    if p_count >= 7:
        assigned_roles = ["Wolf", "Wolf", "Doctor", "Seer", "Seductress", "UmZaki", "King", "Mayor", "Guardian"]

    if len(assigned_roles) > p_count:
        assigned_roles = assigned_roles[:p_count]
        if "Wolf" not in assigned_roles:
            assigned_roles[0] = "Wolf"
    else:
        while len(assigned_roles) < p_count:
            assigned_roles.append("Citizen")

    for index, pid in enumerate(p_ids):
        game["players"][pid]["role"] = assigned_roles[index]

    # زر كشف الهوية الموحد داخل القناة العامة بدون تشويش الخاص
    reveal_view = RevealRoleView(guild_id)
    await ctx.send(
        "📜 **تم توزيع بطاقات الهوية السرية بنجاح بنظام مخفي بالكامل!**\nاضغط على الزر أدناه لمعرفة هويتك وقدرتك الخاصة مباشرة هنا دون أن يراك أحد:",
        view=reveal_view
    )
    
    await asyncio.sleep(10)
    await run_classic_night_phase(ctx, guild_id)

# ==============================================================================
# 10. TRADITIONAL NIGHT LOOP MECHANICS
# ==============================================================================
async def run_classic_night_phase(ctx, guild_id: int):
    game = active_games[guild_id]
    game["status"] = "NIGHT"
    game["night_actions"] = {"kill": None, "save": None, "check": None, "shield": None, "visit": None}
    
    night_view = TraditionalNightActionView(guild_id)
    await ctx.send(
        f"\n🌌 **[المرحلة: الليل - الجولة {game['day_count']}]**\nحلّ الظلام الدامس ونفض الجميع عباءة التعب.. اضغطوا على الزر بالأسفل لتنفيذ حركاتكم السرية مخفياً الحين (المهلة 60 ثانية):",
        view=night_view
    )

    await asyncio.sleep(60)
    await run_classic_day_phase(ctx, guild_id)

# ==============================================================================
# 11. TRADITIONAL DAY BREAK & RESOLUTION LOOP
# ==============================================================================
async def run_classic_day_phase(ctx, guild_id: int):
    game = active_games[guild_id]
    game["status"] = "DAY_ANNOUNCEMENT"
    game["king_execution"] = None
    game["umzaki_expose_id"] = None

    await ctx.send(f"\n☀️ **[المرحلة: الصباح - الجولة {game['day_count']}]**\nأشرقت الشمس واستيقظت القرية لترى ما حدث في عتمة الليل الغامض...")
    await asyncio.sleep(3)

    kill_target = game["night_actions"].get("kill")
    doctor_target = game["night_actions"].get("save")
    guardian_target = game["night_actions"].get("shield")
    seductress_target = game["night_actions"].get("visit")

    seductress_id = next((pid for pid, p in game["players"].items() if p["alive"] and p["role"] == "Seductress"), None)
    
    dead_pool = []
    protected_by_seductress = False

    # معالجة المغوية التقليدية
    if seductress_id and seductress_target:
        if game["players"][seductress_target]["role"] == "Wolf":
            dead_pool.append((seductress_id, "💃 المغرية زارت ذيب بالليل وتمت تصفيتها وتموت معه فوراً!"))
            dead_pool.append((seductress_target, "🐺 الذئب تم جره للموت والهلاك بواسطة المغرية!"))
            game["players"][seductress_id]["alive"] = False
            game["players"][seductress_target]["alive"] = False
        else:
            if kill_target == seductress_target:
                protected_by_seductress = True

    # معالجة هجوم الذئب الكلاسيكي
    if kill_target and game["players"][kill_target]["alive"]:
        is_protected = (kill_target == doctor_target) or (kill_target == guardian_target) or protected_by_seductress
        
        if not is_protected:
            game["players"][kill_target]["alive"] = False
            dead_pool.append((kill_target, "💀 وجده أهل القرية مقتولاً وممزقاً بدم بارد في غرفته بواسطة مخالب الذئاب!"))
            
            # حافز أم زكي الكامن
            if game["players"][kill_target]["role"] == "UmZaki":
                alive_wolves = [pid for pid, p in game["players"].items() if p["alive"] and p["role"] == "Wolf"]
                if alive_wolves:
                    game["umzaki_expose_id"] = random.choice(alive_wolves)

    # طباعة نتائج وفيات الليل بالكامل بالعام مع طقطقة سعودية
    if not dead_pool:
        await ctx.send("💚 **يا للمفاجأة العظمى!** مرت هذه الليلة بسلام تام بدون أي وفيات، والجميع مستيقظ بصحة وعافية.")
    else:
        for pid, txt in dead_pool:
            await ctx.send(f"📢 <@{pid}> {txt}")
            await ctx.send(f"🤫 <@{pid}>: {get_saudi_joke(DEATH_COMMENTS)}")

    if game["umzaki_expose_id"]:
        await ctx.send(f"👵 🩸 **صيحة أم زكي الصادمة!** قبل أن تطلع روح أم زكي كتبت بدمها اسم الذيب وهو: <@{game['umzaki_expose_id']}> !! اشنقوه الحين!")

    if await check_classic_victory(ctx, guild_id):
        return

    # فتح نقاش نهارى 90 ثانية
    game["status"] = "DAY_VOTING"
    game["votes"] = {}

    await ctx.send("\n🗣️ **باب الاتهامات والتحليلات مفتوح الآن بالعام لمدة 90 ثانية!** تشاوروا واكشفوا الخونة.")
    await asyncio.sleep(90)

    # بدء فرز صناديق الاقتراع بالأزرار
    alive_players = [pid for pid, p in game["players"].items() if p["alive"]]
    voting_options = [discord.SelectOption(label=game["players"][pid]["name"], value=str(pid)) for pid in alive_players]

    voting_view = TraditionalDayVotingView(voting_options, guild_id)
    await ctx.send("🗳️ **بدأ وقت التصويت الرسمي والملك يقدر يستخدم سلطته الحين عبر الزر!** (المهلة 45 ثانية):", view=voting_view)
    await asyncio.sleep(45)

    # معالجة خيار إعدام الملك الحاسم أولاً
    if game["king_execution"]:
        executed_id = game["king_execution"]
        game["players"][executed_id]["alive"] = False
        role_label = "ذيب غدار 🐺" if game["players"][executed_id]["role"] == "Wolf" else "مواطن مسكين 🧑‍🌾"
        await ctx.send(f"👑 ⚔️ **بأمر ملكي سامي وقاطع لا رجعة فيه!** تم إعدام اللاعب <@{executed_id}> فوراً خارج أسوار القرية، وتبين أنه كان: **{role_label}**!")
        await ctx.send(f"📢 <@{executed_id}>: {get_saudi_joke(DEATH_COMMENTS)}")
    else:
        if not game["votes"]:
            await ctx.send("⏰ انتهى الوقت ولم يقم أحد بالتصويت! قرر أهل القرية النوم بدون طرد أحد اليوم.")
        else:
            vote_tally = {}
            for voter_id, target_id in game["votes"].items():
                weight = 2 if game["players"][voter_id]["role"] == "Mayor" else 1
                vote_tally[target_id] = vote_tally.get(target_id, 0) + weight

            max_votes = max(vote_tally.values())
            highest_targets = [tid for tid, count in vote_tally.items() if count == max_votes]

            if len(highest_targets) > 1:
                await ctx.send("⚖️ **تعادل في الأصوات المرفوعة بالصندوق!** انقسمت الآراء وخاف أهل القرية من طرد بريء فلم يطردوا أحداً.")
            else:
                voted_out_id = highest_targets[0]
                game["players"][voted_out_id]["alive"] = False
                role_label = "ذيب غدار 🐺" if game["players"][voted_out_id]["role"] == "Wolf" else "مواطن 🧑‍🌾"
                await ctx.send(f"⚖️ **بأغلبية الأصوات الصريحة!** قرر أهل القرية طرد اللاعب <@{voted_out_id}> وشنقه علناً، وطلع دوره هو: **{role_label}**!")
                await ctx.send(f"📢 <@{voted_out_id}>: {get_saudi_joke(DEATH_COMMENTS)}")

    if await check_classic_victory(ctx, guild_id):
        return

    game["day_count"] += 1
    await run_classic_night_phase(ctx, guild_id)

# ==============================================================================
# 12. TRADITIONAL VICTORY CHECK WITH BASIC SCORE ASSIGNMENT
# ==============================================================================
async def check_classic_victory(ctx, guild_id: int) -> bool:
    game = active_games[guild_id]
    
    wolves_count = sum(1 for pid, p in game["players"].items() if p["alive"] and p["role"] == "Wolf")
    good_count = sum(1 for pid, p in game["players"].items() if p["alive"] and p["role"] != "Wolf")

    if wolves_count >= good_count:
        await ctx.send(f"\n🏆 🎉 **قيم اوفر! انتصرت جبهة الذيابة الغدارة والتهتموا القرية بالكامل (Wolves Win)!**")
        await ctx.send(f"📢 البوت يطقطق: {get_saudi_joke(WOLF_WIN_COMMENTS)}")
        
        for pid, p in game["players"].items():
            if p["role"] == "Wolf":
                np = await db.add_points(pid, 60)
                await ctx.send(f"💰 حصل الذئب البطل <@{pid}> على `60` نقطة انتصار!")
                if np >= 500: await ctx.send(f"📢 <@{pid}> {get_saudi_joke(MILESTONE_COMMENTS)}")
                
        del active_games[guild_id]
        return True

    if wolves_count == 0:
        await ctx.send(f"\n🏆 🎉 **قيم اوفر! فازت جبهة القرية والأخيار وتم سحق وطرد الذيابة كلياً (Village Wins)!**")
        await ctx.send(f"📢 البوت يطقطق: {get_saudi_joke(VILLAGE_WIN_COMMENTS)}")
        
        for pid, p in game["players"].items():
            if p["role"] != "Wolf":
                np = await db.add_points(pid, 45)
                await ctx.send(f"💰 حصل البطل الصالح <@{pid}> على `45` نقطة انتصار وتطهير!")
                if np >= 500: await ctx.send(f"📢 <@{pid}> {get_saudi_joke(MILESTONE_COMMENTS)}")
                
        del active_games[guild_id]
        return True

    return False

# ==============================================================================
# 13. BOOT UP EVENTS
# ==============================================================================
@bot.event
async def on_ready():
    await db.initialize()
    # تسجيل الأزرار التفاعلية الدائمة بالـ Gateway لضمان عدم توقفها عند الريستارت
    bot.add_view(TraditionalLobbyView(0))
    bot.add_view(RevealRoleView(0))
    bot.add_view(TraditionalNightActionView(0))
    print(f"==================================================")
    print(f"✅ TRADITIONAL WEREWOLF COG IS ONLINE: {bot.user}")
    print(f"🔥 Standalone Game Engine with all 9 Roles Activated Perfectly!")
    print(f"==================================================")

# حط تالتوكن حقك هنا وشغله علطول!
if __name__ == "__main__":
    # bot.run("MTUwNTg4MzA5NDI5NTc3NzM5MQ.GG8ScK.wFlVlx_NOvOqOQdNGWChTNz44Jo7npSCCCgguI")
    pass