import os
import discord
from discord.ext import commands
import aiosqlite
from dotenv import load_dotenv
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
# 2. SQLITE DATABASE CONTROLLER (ECONOMY & STORE)
# ==============================================================================
class DatabaseManager:
    def __init__(self, db_path: str = "vale_pro_werewolf.db"):
        self.db_path = db_path
        self.conn: Optional[aiosqlite.Connection] = None

    async def initialize(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        
        # Economy Table
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                points INTEGER DEFAULT 0,
                bombs_count INTEGER DEFAULT 0,
                shields_count INTEGER DEFAULT 0,
                hacks_count INTEGER DEFAULT 0
            )
        """)
        await self.conn.commit()

    async def get_user_data(self, user_id: int) -> Tuple[int, int, int, int]:
        async with self.conn.execute(
            "SELECT points, bombs_count, shields_count, hacks_count FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row
            return 0, 0, 0, 0

    async def add_points(self, user_id: int, points: int) -> int:
        current = await self.get_user_data(user_id)
        new_points = max(0, current[0] + points)
        await self.conn.execute("""
            INSERT OR REPLACE INTO users (user_id, points, bombs_count, shields_count, hacks_count)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, new_points, current[1], current[2], current[3]))
        await self.conn.commit()
        return new_points

    async def buy_item(self, user_id: int, item_type: str, cost: int) -> bool:
        current = await self.get_user_data(user_id)
        points, bombs, shields, hacks = current
        if points < cost:
            return False
        
        points -= cost
        if item_type == "bomb":
            bombs += 1
        elif item_type == "shield":
            shields += 1
        elif item_type == "hack":
            hacks += 1

        await self.conn.execute("""
            INSERT OR REPLACE INTO users (user_id, points, bombs_count, shields_count, hacks_count)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, points, bombs, shields, hacks))
        await self.conn.commit()
        return True

    async def close(self):
        if self.conn:
            await self.conn.close()

db = DatabaseManager()

# ==============================================================================
# 3. GLOBAL ENGINE STATE FOR ACTIVE GAMES
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
            "umzaki_expose_id": None,
            "registration_message": None
        }
    return active_games[guild_id]

# ==============================================================================
# 4. EXHAUSTIVE SAUDI JOKES & COMMENTS SYSTEM
# ==============================================================================
WOLF_WIN_COMMENTS = [
    "الذيابة عاثوا في الأرض فساداً ونفضوكم نفض! كفو يا ذيابة السيرفر 😎🐺",
    "اشهد انكم ذيابة شقيتوا القرويين والمحققين شق وطيرتوا هيبتهم كلياً 🔥",
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
# 5. UI COMPONENTS (INTERACTIVE LOBBY, NIGHT PANELS & DAY VOTING)
# ==============================================================================
class LobbyView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="انضمام للعبة 🐺", style=discord.style.green, custom_id="join_game_werewolf")
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

    @discord.ui.button(label="انسحاب 🏃‍♂️", style=discord.style.danger, custom_id="leave_game_werewolf")
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

    @discord.ui.button(label="شرح شخصيات اللعبة 📚", style=discord.style.blurple, custom_id="help_game_werewolf")
    async def help_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        help_embed = discord.Embed(title="📚 كتيب قوانين وشخصيات الذئب الاحترافية", color=discord.Color.gold())
        help_embed.add_field(name="🐺 الذيب", value="يحاول التخلص من جميع الشخصيات والسيطرة على اللعبة بالكامل.", inline=False)
        help_embed.add_field(name="🧑‍🌾 القروي", value="شخصية عادية، ما عنده قدرة خاصة لكن يشارك بالتصويت ويكشف الذيابة بالذكاء والتحليل.", inline=False)
        help_embed.add_field(name="🔍 المحقق", value="يقدر يكشف هوية أي لاعب **مرة واحدة فقط** طوال القيم على الخاص.", inline=False)
        help_embed.add_field(name="🛡️ الحارس", value="يعطي درع حماية لأي لاعب ويحميه من القتل **مرة واحدة فقط** بالقيم.", inline=False)
        help_embed.add_field(name="👑 الملك", value="يملك سلطة تحويل جميع الأصوات على لاعب واحد وطرده مباشرة **مرة واحدة فقط** بالقيم عبر زر خاص نهاراً.", inline=False)
        help_embed.add_field(name="🏛️ العمدة", value="صوته أقوى من الجميع، حيث يُحسب التصويت الخاص فيه بـ **صوتين** تلقائياً في الفرز.", inline=False)
        help_embed.add_field(name="⚕️ الطبيب", value="يستطيع حماية أي لاعب من القتل طوال القيم، لكن لازم يختار بحذر كل ليلة.", inline=False)
        help_embed.add_field(name="💃 المغرية", value="إذا زارت شخص وكان ذيب تموت معه. أما إذا كان شخص عادي وهجمت عليه الذيابة، فإنها تحميه من القتل.", inline=False)
        help_embed.add_field(name="👵 أم زكي", value="إذا قتلتها الذيابة بالليل، تقوم بفضح أحد الذيابة عشوائياً بعبارة بدمها في الصباح.", inline=False)
        await interaction.response.send_message(embed=help_embed, ephemeral=True)


class NightActionSelect(discord.ui.Select):
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


class VotingSelect(discord.ui.Select):
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


class DayVotingView(discord.ui.View):
    def __init__(self, options_list: List[discord.SelectOption], guild_id: int):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.add_item(VotingSelect(options_list, guild_id))

    @discord.ui.button(label="👑 سلطة الملك الإقصائية", style=discord.style.blurple, custom_id="king_power_btn")
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

        # بناء قائمة اختيار خاصة بالملك للطرد الفوري
        alive_players = [pid for pid, p in game["players"].items() if p["alive"]]
        king_options = []
        for pid in alive_players:
            king_options.append(discord.SelectOption(label=game["players"][pid]["name"], value=str(pid)))

        class KingSelectView(discord.ui.View):
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

        await interaction.response.send_message("👑 اختر من القائمة أدناه الشخص الذي تريد تحويل كل الأصوات ضده وطرده حالاً:", view=KingSelectView(self.guild_id, interaction.user.id), ephemeral=True)

# ==============================================================================
# 6. ECONOMY COMMANDS & ADVANTAGES SYSTEM
# ==============================================================================
@bot.command(name="نقاطي")
async def my_points(ctx):
    points, bombs, shields, hacks = await db.get_user_data(ctx.author.id)
    embed = discord.Embed(title=f"📊 محفظة {ctx.author.display_name}", color=discord.Color.blue())
    embed.add_field(name="النقاط الكلية 💰", value=f"`{points}` نقطة", inline=False)
    embed.add_field(name="القنابل 💣", value=f"`{bombs}` قنبلة", inline=True)
    embed.add_field(name="الدروع (المنع) 🛡️", value=f"`{shields}` درع حماية", inline=True)
    embed.add_field(name="أدوات التهكير ⚡", value=f"`{hacks}` أداة تهكير", inline=True)
    embed.set_footer(text="سيرفر Vale Community الفخم والعتيد")
    await ctx.send(embed=embed)

@bot.command(name="المتجر")
async def shop(ctx):
    embed = discord.Embed(title="🛒 متجر ميزات سيرفر Vale التكتيكي", color=discord.Color.gold())
    embed.add_field(name="1. شراء قنبلة 💣 (`!شراء_قنبلة`)", value="السعر: `50` نقطة\nتستخدم لتفجير جولات الخصوم وتخريب نقاطهم.", inline=False)
    embed.add_field(name="2. شراء درع منع 🛡️ (`!شراء_درع`)", value="السعر: `40` نقطة\nيحميك تلقائياً من الطرد بالنهار أو القتل بالليل لمرة واحدة.", inline=False)
    embed.add_field(name="3. شراء أداة تهكير ⚡ (`!شراء_تهكير`)", value="السعر: `75` نقطة\nلسرقة نقاط عشوائية تكتيكية من محفظة شخص آخر.", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="شراء_قنبلة")
async def buy_bomb(ctx):
    success = await db.buy_item(ctx.author.id, "bomb", 50)
    if success:
        await ctx.send(f"✅ تم شراء قنبلة بنجاح يا {ctx.author.mention}! خصومك في خطر الحين 💣")
    else:
        await ctx.send(f"❌ نقاطك ما تكفي يا حب! تحتاج 50 نقطة كاش.")

@bot.command(name="شراء_درع")
async def buy_shield(ctx):
    success = await db.buy_item(ctx.author.id, "shield", 40)
    if success:
        await ctx.send(f"✅ تم شراء درع منع وحماية بنجاح {ctx.author.mention}! الحين وضعك سليم 🛡️")
    else:
        await ctx.send(f"❌ نقاطك غير كافية، تحتاج 40 نقطة في محفظتك.")

@bot.command(name="شراء_تهكير")
async def buy_hack(ctx):
    success = await db.buy_item(ctx.author.id, "hack", 75)
    if success:
        await ctx.send(f"✅ تم شراء أداة تهكير بنجاح {ctx.author.mention}! روح هكر الهوامير ⚡")
    else:
        await ctx.send(f"❌ نقاطك غير كافية، تحتاج 75 نقطة كاش.")

@bot.command(name="تهكير")
async def hack_player(ctx, target: discord.Member):
    if target.id == ctx.author.id:
        await ctx.send("تهكر نفسك؟ هههههههه حدث العاقل بما يعقل يا بعد راسي 😂")
        return
    
    points, bombs, shields, hacks = await db.get_user_data(ctx.author.id)
    if hacks < 1:
        await ctx.send("❌ ما عندك أدوات تهكير! روح المتجر واشتر وحدة أولاً لتبدأ الجريمة.")
        return

    await db.conn.execute("UPDATE users SET hacks_count = hacks_count - 1 WHERE user_id = ?", (ctx.author.id,))
    t_points, t_bombs, t_shields, t_hacks = await db.get_user_data(target.id)
    
    if t_points <= 0:
        await ctx.send(f"⚡ حاولت تهكر {target.display_name} بس طلع طفران على الحديدة وما عنده شيء! راحت عليك الأداة هههههههه")
        await db.conn.commit()
        return

    stolen = random.randint(10, min(t_points, 100))
    await db.add_points(target.id, -stolen)
    new_p = await db.add_points(ctx.author.id, stolen)
    
    await ctx.send(f"🏴‍☠️ **عملية تهكير ناجحة!** {ctx.author.mention} سرق `{stolen}` نقطة من محفظة {target.mention} وضبط وضعه كلياً!")
    if new_p >= 500:
        await ctx.send(f"📢 {ctx.author.mention} {get_saudi_joke(MILESTONE_COMMENTS)}")

# ==============================================================================
# 7. CORE CORE WEREWOLF LOBBY ENGINE
# ==============================================================================
@bot.command(name="ذيب")
async def open_werewolf_lobby(ctx):
    guild_id = ctx.guild.id
    game = get_game_state(guild_id)
    
    if game["status"] != "LOBBY":
        await ctx.send("في قيم ذيب شغال حالياً بالسيرفر، انتظر ينتهي أو اكتب أمر التصفير!")
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
        title="🐺 نظام لعبة الذئب الاحترافي الشامل - مجتمع Vale",
        description=f"**المضيف:** {ctx.author.mention}\n\n**قائمة المشتركين الحالية (1):**\n• {ctx.author.mention}",
        color=discord.Color.dark_purple()
    )
    embed.set_image(url="https://images.unsplash.com/photo-1590424753042-35677df1461f?q=80&w=600")
    embed.set_footer(text="اضغط على الأزرار بالأسفل لإدارة اشتراكك أو قراءة الكتيب")

    view = LobbyView(guild_id)
    game["registration_message"] = await ctx.send(embed=embed, view=view)

@bot.command(name="ابدأ_الذيب")
async def start_werewolf_game_pro(ctx):
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
    await ctx.send("📜 **جاري توزيع بطاقات الهوية والخصائص السرية الـ 9 على الخاص... استعدوا للتحركات الليلية!**")
    
    p_ids = list(game["players"].keys())
    random.shuffle(p_ids)
    
    # قائمة الأدوار المتاحة بالكامل حسب ترتيب الأولوية القصوى لمنع النقص
    roles_priority = ["Wolf", "Doctor", "Seer", "Seductress", "UmZaki", "King", "Mayor", "Guardian"]
    
    assigned_roles = []
    # إذا كان العدد كبير نزيد عدد الذئاب تلقائياً لوزن اللعبة
    if p_count >= 7:
        assigned_roles = ["Wolf", "Wolf", "Doctor", "Seer", "Seductress", "UmZaki", "King", "Mayor", "Guardian"]
    else:
        assigned_roles = ["Wolf", "Doctor", "Seer", "Seductress", "UmZaki", "King", "Mayor", "Guardian"]

    # قص أو تعبئة المواطنين بناء على عدد اللاعبين
    if len(assigned_roles) > p_count:
        assigned_roles = assigned_roles[:p_count]
        if "Wolf" not in assigned_roles:
            assigned_roles[0] = "Wolf"
    else:
        while len(assigned_roles) < p_count:
            assigned_roles.append("Citizen")

    # توزيع الأدوار وإرسال شرح القوة لكل لاعب سرياً في الخاص لعدم التخريب
    for index, pid in enumerate(p_ids):
        role = assigned_roles[index]
        game["players"][pid]["role"] = role
        member = ctx.guild.get_member(pid)
        
        if member:
            try:
                if role == "Wolf":
                    await member.send("Wolf 🐺 | **هويتك:** أنت **الذيب**!\nهدفك التخلص من جميع الشخصيات والسيطرة على اللعبة بالكامل بالتنسيق مع بقية الذيابة.")
                elif role == "Citizen":
                    await member.send("Citizen 🧑‍🌾 | **هويتك:** أنت **قروي صالح**!\nما عندك قدرة خاصة لكن تشارك بالنهار وتكشف الذيابة بذكائك وتحليلك.")
                elif role == "Seer":
                    await member.send("Detective 🔍 | **هويتك:** أنت **المحقق**!\nتقدر تكشف هوية وأي دور لاعب **مرة واحدة فقط طوال القيم** بالليل عبر الخيارات التفاعلية.")
                elif role == "Guardian":
                    await member.send("Guardian 🛡️ | **هويتك:** أنت **الحارس**!\nتقدر تعطي درع حماية لأي لاعب وتحميه من القتل **مرة واحدة فقط بالقيم** بالليل.")
                elif role == "King":
                    await member.send("King 👑 | **هويتك:** أنت **الملك**!\nتملك سلطة تحويل جميع أصوات النهار على لاعب واحد وطرده فوراً **مرة واحدة فقط بالقيم** عبر زر مخصص.")
                elif role == "Mayor":
                    await member.send("Mayor 🏛️ | **هويتك:** أنت **العمدة**!\nصوتك أقوى من الجميع، حيث يُحسب التصويت الخاص فيك بـ **صوتين تلقائياً** في الفرز النهائي.")
                elif role == "Doctor":
                    await member.send("Doctor ⚕️ | **هويتك:** أنت **الطبيب**!\nتستطيع حماية أي لاعب من القتل طوال القيم، تختار هدفك الطبي كل ليلة بعناية وحذر.")
                elif role == "Seductress":
                    await member.send("Seductress 💃 | **هويتك:** أنتِ **المغرية**!\nإذا زرتِ شخص وكان ذيب تموتين معه فوراً، أما إذا زرتِ شخص عادي وهجمت عليه الذيابة تحمينه من الموت!")
                elif role == "UmZaki":
                    await member.send("UmZaki 👵 | **هويتك:** أنتِ **أم زكي**!\nقدرة كامنة: إذا قتلتكِ الذيابة بالليل، تقومين بفضح اسم أحد الذيابة عشوائياً بعبارة دم في الصباح الكاشف!")
            except discord.Forbidden:
                await ctx.send(f"⚠️ يرجى فتح الخاص يا <@{pid}> لكي تلعب معنا بكامل المتعة والجولات القادمة!")

    await run_night_phase_pro(ctx, guild_id)

# ==============================================================================
# 8. THE COMPLETE NIGHT PHASE LOOP WITH ROLE INTERACTIONS
# ==============================================================================
async def run_night_phase_pro(ctx, guild_id: int):
    game = active_games[guild_id]
    game["status"] = "NIGHT"
    game["night_actions"] = {"kill": None, "save": None, "check": None, "shield": None, "visit": None}
    
    await ctx.send(f"\n🌌 **[المرحلة: الليل - اليوم {game['day_count']}]**\nحلّ الظلام الدامس.. نامت القرية وبدأت الأدوار السرية باتخاذ قراراتها الفتاكة عبر الخاص الحين!")

    alive_players = [pid for pid, p in game["players"].items() if p["alive"]]
    standard_options = []
    for pid in alive_players:
        standard_options.append(discord.SelectOption(label=game["players"][pid]["name"], value=str(pid)))

    # إرسال الخيارات والقوائم التفاعلية للأدوار الحية ليلاً
    for pid, p in game["players"].items():
        if not p["alive"]:
            continue
        member = ctx.guild.get_member(pid)
        if not member:
            continue

        if p["role"] == "Wolf":
            view = discord.ui.View(timeout=60.0).add_item(NightActionSelect("WOLF", standard_options, guild_id))
            try:
                await member.send("🐺 **الافتراس:** اختر الشخص الذي تريد التخلص منه وذبحه الليلة:", view=view)
            except: pass
        elif p["role"] == "Doctor":
            view = discord.ui.View(timeout=60.0).add_item(NightActionSelect("DOCTOR", standard_options, guild_id))
            try:
                await member.send("⚕️ **الرعاية الطبية:** اختر اللاعب الذي تريد حمايته وإنقاذه الليلة:", view=view)
            except: pass
        elif p["role"] == "Seer" and not p["has_used_power"]:
            view = discord.ui.View(timeout=60.0).add_item(NightActionSelect("SEER", standard_options, guild_id))
            try:
                await member.send("🔍 **التحري (مرة واحدة):** اختر الشخص الذي تريد كشف دوره الحقيقي السري الآن:", view=view)
            except: pass
        elif p["role"] == "Guardian" and not p["has_used_power"]:
            view = discord.ui.View(timeout=60.0).add_item(NightActionSelect("GUARDIAN", standard_options, guild_id))
            try:
                await member.send("🛡️ **درع الحارس (مرة واحدة):** اختر الشخص الذي تريد منحه الحصانة المطلقة الليلة:", view=view)
            except: pass
        elif p["role"] == "Seductress":
            view = discord.ui.View(timeout=60.0).add_item(NightActionSelect("SEDUCTRESS", standard_options, guild_id))
            try:
                await member.send("💃 **الغواية:** اختاري الشخص الذي تودين زيارته الليلة تكتيكياً:", view=view)
            except: pass

    await ctx.send("⏰ *متبقي 60 ثانية لتلقي كل الحركات الليلية والقرارات السرية...*")
    await asyncio.sleep(60)
    
    await run_day_phase_pro(ctx, guild_id)

# ==============================================================================
# 9. THE COMPLETE DAY PHASE LOOP & COMPREHENSIVE COMBAT RESOLUTION
# ==============================================================================
async def run_day_phase_pro(ctx, guild_id: int):
    game = active_games[guild_id]
    game["status"] = "DAY_ANNOUNCEMENT"
    game["king_execution"] = None
    game["umzaki_expose_id"] = None

    await ctx.send(f"\n☀️ **[المرحلة: الصباح - اليوم {game['day_count']}]**\nأشرقت الشمس واستيقظت القرية لترى ما حدث في عتمة الليل الغامض...")
    await asyncio.sleep(2)

    # جرد التحركات الليلية المسجلة في الذاكرة
    kill_target = game["night_actions"].get("kill")
    doctor_target = game["night_actions"].get("save")
    guardian_target = game["night_actions"].get("shield")
    seductress_target = game["night_actions"].get("visit")

    seductress_id = next((pid for pid, p in game["players"].items() if p["alive"] and p["role"] == "Seductress"), None)
    
    dead_pool = []
    protected_by_seductress = False

    # 1. معالجة زيارة المغوية (إذا زارت ذيب تموت معه)
    if seductress_id and seductress_target:
        target_role = game["players"][seductress_target]["role"]
        if target_role == "Wolf":
            dead_pool.append((seductress_id, "💃 المغرية زارت ذيب بالليل وتمت تصفيتها وتموت معه فوراً!"))
            dead_pool.append((seductress_target, "🐺 الذئب تم جره للموت والهلاك بواسطة المغرية!"))
            game["players"][seductress_id]["alive"] = False
            game["players"][seductress_target]["alive"] = False
        else:
            # إذا زارت شخص عادي وهجمت عليه الذيابة تحميه
            if kill_target == seductress_target:
                protected_by_seductress = True

    # 2. معالجة هجوم الذئب والتحقق من آليات الحماية الـ 4
    if kill_target and game["players"][kill_target]["alive"]:
        is_protected = (kill_target == doctor_target) or (kill_target == guardian_target) or protected_by_seductress
        
        # فحص إضافي لدرع الحماية المشترى من المتجر كحماية أخيرة للمحترفين
        points, bombs, shields, hacks = await db.get_user_data(kill_target)
        if not is_protected and shields > 0:
            await db.conn.execute("UPDATE users SET shields_count = shields_count - 1 WHERE user_id = ?", (kill_target,))
            await db.conn.commit()
            is_protected = True
            await ctx.send(f"🛡️ حاول الذئب قتل <@{kill_target}> ولكن درع المنع المشتري من المتجر حماه!")

        if not is_protected:
            game["players"][kill_target]["alive"] = False
            dead_pool.append((kill_target, "💀 وجده أهل القرية مقتولاً وممزقاً بدم بارد في غرفته بواسطة مخالب الذئاب!"))
            
            # 3. معالجة قدرة أم زكي إذا كانت هي الضحية المقتولة بواسطة الذئب
            if game["players"][kill_target]["role"] == "UmZaki":
                alive_wolves = [pid for pid, p in game["players"].items() if p["alive"] and p["role"] == "Wolf"]
                if alive_wolves:
                    game["umzaki_expose_id"] = random.choice(alive_wolves)

    # الإعلان الرسمي عن وفيات ومجريات الليلة الشاملة في التشات العام
    if not dead_pool:
        await ctx.send("💚 **يا للمفاجأة العظمى!** مرت هذه الليلة بسلام تام بدون أي وفيات، والجميع مستيقظ بصحة وعافية.")
    else:
        for pid, txt in dead_pool:
            await ctx.send(f"📢 <@{pid}> {txt}")
            await ctx.send(f"🤫 <@{pid}>: {get_saudi_joke(DEATH_COMMENTS)}")

    # الإعلان عن فضح أم زكي للذيب بدمها إن وُجدت
    if game["umzaki_expose_id"]:
        await ctx.send(f"👵 🩸 **صيحة أم زكي الصادمة!** قبل أن تطلع روح أم زكي كتبت بدمها على الجدار اسم الذيب الغدار وهو: <@{game['umzaki_expose_id']}> !! اشنقوه الحين!")

    if await check_victory_pro(ctx, guild_id):
        return

    # الانتقال لفترة النقاش والتصويت العلني
    game["status"] = "DAY_VOTING"
    game["votes"] = {}

    await ctx.send("\n🗣️ **باب الاتهامات والنقاش مفتوح الآن بالعام لمدة 90 ثانية!** تشاوروا واكشفوا الذيابة.")
    await asyncio.sleep(90)

    # بناء خيارات التصويت النهاري بالأزرار
    alive_players = [pid for pid, p in game["players"].items() if p["alive"]]
    voting_options = []
    for pid in alive_players:
        voting_options.append(discord.SelectOption(label=game["players"][pid]["name"], value=str(pid)))

    view = DayVotingView(voting_options, guild_id)
    await ctx.send("🗳️ **بدأ وقت التصويت الرسمي والملك يقدر يستخدم سلطته الحين عبر الزر!** (المهلة 45 ثانية):", view=view)
    await asyncio.sleep(45)

    # 4. التحقق أولاً من سلطة الملك الإقصائية الفورية الحارقة
    if game["king_execution"]:
        executed_id = game["king_execution"]
        game["players"][executed_id]["alive"] = False
        role_label = "ذيب غدار 🐺" if game["players"][executed_id]["role"] == "Wolf" else "مواطن مسكين 🧑‍🌾"
        await ctx.send(f"👑 ⚔️ **بأمر ملكي سامي وقاطع لا رجعة فيه!** تم إعدام وطرد اللاعب <@{executed_id}> فوراً خارج أسوار القرية، وتبين أنه كان: **{role_label}**!")
        await ctx.send(f"📢 <@{executed_id}>: {get_saudi_joke(DEATH_COMMENTS)}")
    else:
        # فرز الأصوات العادية مع احتساب صوت العمدة بصوتين (2)
        if not game["votes"]:
            await ctx.send("⏰ انتهى الوقت ولم يقم أحد بالتصويت! قرر أهل القرية النوم بدون طرد أي شخص اليوم.")
        else:
            vote_tally = {}
            for voter_id, target_id in game["votes"].items():
                # آلية العمدة يُحسب صوته بصوتين
                weight = 2 if game["players"][voter_id]["role"] == "Mayor" else 1
                vote_tally[target_id] = vote_tally.get(target_id, 0) + weight

            max_votes = max(vote_tally.values())
            highest_targets = [tid for tid, count in vote_tally.items() if count == max_votes]

            if len(highest_targets) > 1:
                await ctx.send("⚖️ **تعادل في الأصوات المرفوعة بالصندوق!** انقسمت الآراء وخاف أهل القرية من طرد بريء فلم يطردوا أحداً.")
            else:
                voted_out_id = highest_targets[0]
                
                # فحص درع منع المتجر من الطرد الجماعي
                v_points, v_bombs, v_shields, v_hacks = await db.get_user_data(voted_out_id)
                if v_shields > 0:
                    await db.conn.execute("UPDATE users SET shields_count = shields_count - 1 WHERE user_id = ?", (voted_out_id,))
                    await db.conn.commit()
                    await ctx.send(f"🛡️ صوّتت الأغلبية لطرد <@{voted_out_id}> ولكن درع الحماية المشتري من المتجر صد الإقصاء ومنحه فرصة أخرى!")
                else:
                    game["players"][voted_out_id]["alive"] = False
                    role_label = "ذيب غدار 🐺" if game["players"][voted_out_id]["role"] == "Wolf" else "مواطن 🧑‍🌾"
                    await ctx.send(f"⚖️ **بأغلبية الأصوات!** قرر أهل القرية طرد اللاعب <@{voted_out_id}> وشنقه علناً، وطلع دوره هو: **{role_label}**!")
                    await ctx.send(f"📢 <@{voted_out_id}>: {get_saudi_joke(DEATH_COMMENTS)}")

    if await check_victory_pro(ctx, guild_id):
        return

    # جولة جديدة تلقائية
    game["day_count"] += 1
    await run_night_phase_pro(ctx, guild_id)

# ==============================================================================
# 10. VICTORY CALCULATION WITH ECO REWARDS
# ==============================================================================
async def check_victory_pro(ctx, guild_id: int) -> bool:
    game = active_games[guild_id]
    
    wolves_count = sum(1 for pid, p in game["players"].items() if p["alive"] and p["role"] == "Wolf")
    good_count = sum(1 for pid, p in game["players"].items() if p["alive"] and p["role"] != "Wolf")

    if wolves_count >= good_count:
        await ctx.send(f"\n🏆 🎉 **قيم اوفر! انتصرت جبهة الذيابة الغدارة والتهتموا القرية بالكامل (Wolves Win)!**")
        await ctx.send(f"📢 البوت يطقطق: {get_saudi_joke(WOLF_WIN_COMMENTS)}")
        
        for pid, p in game["players"].items():
            if p["role"] == "Wolf":
                np = await db.add_points(pid, 60)
                await ctx.send(f"💰 حصل الذئب البطل <@{pid}> على `60` نقطة كاش فوز!")
                if np >= 500: await ctx.send(f"📢 <@{pid}> {get_saudi_joke(MILESTONE_COMMENTS)}")
                
        del active_games[guild_id]
        return True

    if wolves_count == 0:
        await ctx.send(f"\n🏆 🎉 **قيم اوفر! فازت جبهة القرية والأخيار وتم سحق وطرد الذيابة كلياً (Village Wins)!**")
        await ctx.send(f"📢 البوت يطقطق: {get_saudi_joke(VILLAGE_WIN_COMMENTS)}")
        
        for pid, p in game["players"].items():
            if p["role"] != "Wolf":
                np = await db.add_points(pid, 45)
                await ctx.send(f"💰 حصل البطل الصالح <@{pid}> على `45` نقطة كاش تطهير!")
                if np >= 500: await ctx.send(f"📢 <@{pid}> {get_saudi_joke(MILESTONE_COMMENTS)}")
                
        del active_games[guild_id]
        return True

    return False

# ==============================================================================
# 11. EMERGENCY RECYCLING COMMAND
# ==============================================================================
@bot.command(name="تصفير_الذيب")
async def pro_emergency_reset(ctx):
    guild_id = ctx.guild.id
    if guild_id in active_games:
        del active_games[guild_id]
    await ctx.send("♻️ **تم تصفير الروم بالكامل ومسح البيانات النشطة!** الساحة جاهزة الحين لفتح تسجيل جيم جديد ومحترف بدون أخطاء.")

# ==============================================================================
# 12. RUNNING EVENTS
# ==============================================================================
@bot.event
async def on_ready():
    await db.initialize()
    print(f"==================================================")
    print(f"✅ VALE PRO WEREWOLF BOT IS ONLINE: {bot.user}")
    print(f"🔥 All 9 Custom Roles, UI Buttons, and Store Loaded Properly!")
    print(f"==================================================")

@bot.command(name="فحص")
async def test_ping(ctx):
    await ctx.send("🚀 البوت شغال بكامل هوياته الـ 9 وأزرار الملك والشرح التفاعلية وجاهز للجلد الفخم في سيرفر Vale!")

async def main():
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("❌ Error: DISCORD_TOKEN is missing!")
        return
    async with bot:
        try:
            await bot.start(token)
        except KeyboardInterrupt:
            pass
        finally:
            await db.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutdown completed.")