import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import asyncio
import random
import os
from datetime import datetime

# -------------------------------------------------------------------
# 1. DATABASE MANAGEMENT (SQLite with WAL for Render)
# -------------------------------------------------------------------
class Database:
    def __init__(self):
        self.db_path = os.getenv("DATABASE_URL", "vale_production.db")

    async def init(self):
        self.conn = await aiosqlite.connect(self.db_path)
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS economy (
                user_id INTEGER PRIMARY KEY,
                points INTEGER DEFAULT 0
            )
        """)
        await self.conn.commit()

    async def add_points(self, user_id: int, points: int):
        await self.conn.execute("""
            INSERT INTO economy (user_id, points) VALUES (?, ?) 
            ON CONFLICT(user_id) DO UPDATE SET points = points + ?
        """, (user_id, points, points))
        await self.conn.commit()

    async def get_points(self, user_id: int) -> int:
        async with self.conn.execute("SELECT points FROM economy WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def reset_all(self):
        await self.conn.execute("DELETE FROM economy")
        await self.conn.commit()

# -------------------------------------------------------------------
# 2. WEREWOLF GAME ENGINE LOGIC & COG
# -------------------------------------------------------------------
class Role:
    def __init__(self, name, team, desc):
        self.name = name
        self.team = team  # 'wolf' or 'village'
        self.desc = desc

ROLES_DATA = {
    "الذيب": Role("الذيب 🐺", "wolf", "هدفك تاكل القرويين وتجحد التهمة بالعام! كل ليلة تعشّ على واحد مع ربعك!"),
    "القروي": Role("القروي 🧑‍🌾", "village", "مواطن غلبان على باب الله، ما عندك صلاحيات بالليل، شغل مخك بالنهار وصوت صح لا تجيب العيد!"),
    "المحقق": Role("المحقق 🔍", "village", "تقدر تكشف هوية لاعب واحد بالكامل لمرة واحدة في القيم.. شغل المباحث اللي عندك!"),
    "الحارس": Role("الحارس 🛡️", "village", "تقدر تحمي لاعب واحد من الغدر لمرة واحدة في القيم! فكنا من الفضايح وسوّ فزعة!"),
    "الملك": Role("الملك 👑", "village", "عندك سلطة ملكية مطلقة لمرة واحدة بالنهار! تقدر تطير أي لاعب تشك فيه فوراً وتلغي التصويت!"),
    "العمدة": Role("العمدة 🏛️", "village", "صوتك في التصويت النهاري ينحسب عن صوتين تلقائياً، هيبتك تفرق في السيرفر!"),
    "الطبيب": Role("الطبيب ⚕️", "village", "تقدر تختار لاعب تحميه وتعالجه من هجوم الذيابة كل ليلة! لا تموت بس!"),
    "المغرية": Role("المغرية 💃", "village", "تزور لاعب كل ليلة؛ لو طلع ذيب تموتون سوا برومانسية خبيثة، ولو قروي مستهدف تحميه!"),
    "أم زكي": Role("أم زكي 👵", "village", "لو غدروك الذيابة بالليل، البوت بيفضح اسم واحد منهم بالعام الصباح قهر وعناد!")
}

class GameSession:
    def __init__(self, guild_id, channel_id):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.phase = "LOBBY"
        self.players = []
        self.alive = []
        self.roles = {}       
        self.night_actions = {} 
        self.used_powers = set() 
        self.votes = {}       
        self.has_voted = set()
        self.king_executed = False
        self.mayor_id = None

class WerewolfCog(commands.Cog):
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db
        self.games = {}

    @commands.hybrid_command(name="ذيب", description="فتح تسجيل لقرية الذئاب الفخمة والطقطقة")
    async def lobby_cmd(self, ctx: commands.Context):
        g_id = ctx.guild.id
        if g_id in self.games and self.games[g_id].phase != "LOBBY":
            return await ctx.send("❌ فيه قيم شغال حالياً والسيرفر مولع، انتظرهم يخلصون جلد يا بطل!")
        
        self.games[g_id] = GameSession(g_id, ctx.channel.id)
        game = self.games[g_id]
        
        embed = discord.Embed(
            title="🐺 قرية الغدر والذكاء (الذئاب والقرويين) 🐺",
            description="**حياكم الله في قيم الطقطقة والشكوك!**\nاضغط على الأزرار بالأسفل للمشاركة أو الانسحاب قبل ما يقفل التسجيل.",
            color=0x7289da
        )
        embed.add_field(name="👥 قائمة الضحايا (المسجلين حالياً)", value="الروم فاضي.. الكل خايف؟ 🤔", inline=False)
        embed.set_footer(text="يتطلب القيم من 4 إلى 9 لاعبين كحد أقصى.")
        
        view = RegistrationView(self, g_id)
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="ابدأ_الذيب", description="بدء توزيع الأدوار وانطلاق اللعبة")
    async def start_cmd(self, ctx: commands.Context):
        g_id = ctx.guild.id
        if g_id not in self.games or self.games[g_id].phase != "LOBBY":
            return await ctx.send("❌ ما فيه تسجيل مفتوح حالياً، اكتب `/ذيب` أولاً يا ذكي!")
        
        game = self.games[g_id]
        p_count = len(game.players)
        if p_count < 4 or p_count > 9:
            return await ctx.send(f"❌ العدد سكه! المسجلين حالياً: **{p_count}**. لازم يكون بين 4 و 9 لاعبين عشان تبدأ المتعة.")
        
        game.phase = "DISTRIBUTING"
        pool = ["الذيب"] + list(ROLES_DATA.keys())[2:p_count+1]
        while len(pool) < p_count:
            pool.append("القروي")
        
        random.shuffle(pool)
        for i, p_id in enumerate(game.players):
            game.roles[p_id] = ROLES_DATA[pool[i]]
            game.alive.append(p_id)
            if pool[i] == "العمدة":
                game.mayor_id = p_id
            
        embed = discord.Embed(
            title="🕵️‍♂️ تم توزيع الأدوار بالسرية التامة!",
            description="كل لاعب يضغط على الزر بالأسفل لمعرفة هويته السرية عبر رسالة مخفية!\n**الرجاء عدم الفضائح والـ Meta-gaming!**",
            color=0xe74c3c
        )
        await ctx.send(embed=embed, view=RevealRoleView(game))
        await asyncio.sleep(10)
        await self.run_night_phase(ctx, game)

    async def run_night_phase(self, ctx, game):
        game.phase = "NIGHT"
        game.night_actions = {}
        
        embed = discord.Embed(
            title="🌙 يحل الليل وتنام القرية... وعيون الخونة تصحى!",
            description="على أصحاب القدرات الليلية الضغط على الزر بالأسفل لتنفيذ تحركاتهم السرية (معك 60 ثانية قبل الصباح)!",
            color=0x1a237e
        )
        await ctx.send(embed=embed, view=NightActionPanel(self, game))
        await asyncio.sleep(60)
        await self.resolve_night_and_cycle(ctx, game)

    async def resolve_night_and_cycle(self, ctx, game):
        game.phase = "DAY_ANNOUNCEMENT"
        
        wolf_target = game.night_actions.get("kill")
        doc_target = game.night_actions.get("heal")
        guard_target = game.night_actions.get("protect")
        sed_target = game.night_actions.get("block")
        
        # Seductress checks
        if sed_target and game.roles.get(sed_target) and game.roles[sed_target].team == "wolf":
            sed_id = [p for p in game.alive if "المغرية" in game.roles[p].name]
            if sed_id:
                s_id = sed_id[0]
                if s_id in game.alive: game.alive.remove(s_id)
                if sed_target in game.alive: game.alive.remove(sed_target)
                await ctx.send(f"💥 **أكشن نص الليل!** المغرية 💃 زارت لاعب طلع ذيب 🐺! وماتوا الاثنين سوا بالخناق هههههه دمرتهم الرومانسية!")
        
        killed_id = None
        if wolf_target and wolf_target != sed_target:
            if wolf_target != doc_target and wolf_target != guard_target:
                killed_id = wolf_target
                if killed_id in game.alive:
                    game.alive.remove(killed_id)

        # check Um Zaki passive trigger
        um_zaki_reveal = ""
        if killed_id and "أم زكي" in game.roles[killed_id].name:
            wolves = [p for p in game.alive if game.roles[p].team == "wolf"]
            if wolves:
                exposed_wolf = ctx.guild.get_member(random.choice(wolves))
                um_zaki_reveal = f"\n👵 **صياح وهز لسان أم زكي قبل تموت:** فضحت الذيب **{exposed_wolf.mention}** وقالت للقرية هذا خاين وشرير أعرف ريحته! 🤫"

        embed = discord.Embed(title="☀️ طلعت الشمس وصحيوا القرويين الطيبين", color=0xf1c40f)
        if killed_id:
            victim = ctx.guild.get_member(killed_id)
            embed.description = f"💀 **يا فرحة ما تمت!** قمنا اليوم على جثة المرحوم {victim.mention}.. أكلوه الذيابة ومسحوا به البلاط! {um_zaki_reveal}"
        else:
            embed.description = "🛡️ **يا لطيف!** ليلة هادية وما انقتل أحد.. يا الطبيب يا الحارس مسوين شغل جامد وفزعة كفو!"
            
        await ctx.send(embed=embed)

        if self.check_win(ctx, game): return
        
        game.phase = "DAY_VOTING"
        game.votes = {}
        game.has_voted = set()
        game.king_executed = False
        
        await ctx.send("📢 **بدأ وقت النقاش والجلد والاتهامات السكه!** سولفوا واصطادوا الخونة ثم ابدأوا التصويت بالأسفل!")
        await ctx.send("🗳️ **لوحة التصويت العلنية وسلطة الملك:**", view=DayVotingView(self, game))
        await asyncio.sleep(45)
        
        if not game.king_executed:
            await self.resolve_day_votes(ctx, game)

    async def resolve_day_votes(self, ctx, game):
        if not game.votes:
            await ctx.send("💤 النوم سلطان! ما حد صوت لأحد، تمر الجلسة بسلام وبدون صلب.")
        else:
            highest_vote = max(game.votes.values())
            candidates = [p_id for p_id, count in game.votes.items() if count == highest_vote]
            
            final_target = candidates[0]
            if len(candidates) > 1 and game.mayor_id and game.mayor_id in game.alive:
                final_target = game.mayor_id  # Mayor preference or ties choice safety
                await ctx.send(f"🏛️ **العمدة دخل بالخط:** بسبب التعادل، هيبة وصوت العمدة رجحت الكفة!")
            
            if final_target in game.alive:
                game.alive.remove(final_target)
                target_user = ctx.guild.get_member(final_target)
                await ctx.send(f"⚰️ **بحكم الشعب والقرية والمحكمة التكروشية:** تم صلب {target_user.mention}! وهويته كانت: **{game.roles[final_target].name}** .. ارتاحوا منه!")
                
        if not self.check_win(ctx, game):
            await self.run_night_phase(ctx, game)

    def check_win(self, ctx, game):
        wolves = [p for p in game.alive if game.roles[p].team == "wolf"]
        villagers = [p for p in game.alive if game.roles[p].team == "village"]
        
        if len(wolves) == 0:
            asyncio.create_task(self.end_match(ctx, game, "village"))
            return True
        if len(wolves) >= len(villagers):
            asyncio.create_task(self.end_match(ctx, game, "wolf"))
            return True
        return False

    async def end_match(self, ctx, game, winner_team):
        if winner_team == "wolf":
            msg = "🐺 **عاشوا الذيابة!** انتصر الشر وجحدوا التهمة وتمت تصفية القرية بنجاح! كل ذيب حي يربح **60 نقطة**."
            pts = 60
            winners = [p for p in game.alive if game.roles[p].team == "wolf"]
        else:
            msg = "🧑‍🌾 **كفووو يا قرويين يا شقردية!** صدتوهم ونظفتوا الديرة من الأشكال المريبة! كل قروي حي يربح **45 نقطة**."
            pts = 45
            winners = [p for p in game.alive if game.roles[p].team == "village"]
            
        for w_id in winners:
            await self.db.add_points(w_id, pts)
            
        embed = discord.Embed(title="🏁 نهاية المباراة الرسمية والمصخرة", description=msg, color=0x2ecc71)
        for p_id in game.players:
            user = ctx.guild.get_member(p_id)
            status = "❤️ حي كفو" if p_id in game.alive else "💀 ميت منتهي"
            embed.add_field(name=user.display_name if user else f"لاعب {p_id}", value=f"الدور: {game.roles[p_id].name} | {status}", inline=True)
            
        await ctx.send(embed=embed)
        if ctx.guild.id in self.games:
            del self.games[ctx.guild.id]

    @commands.hybrid_command(name="نقاطي", description="مشاهدة رصيد نقاطك بالقيم وتصنيفك وطقطقة السيرفر")
    async def points_cmd(self, ctx: commands.Context):
        pts = await self.db.get_points(ctx.author.id)
        comment = "شد حيلك يا قروي لسه توك مبتدئ وما جمعت شيء! 👶"
        if pts > 500:
            comment = "يا ساتر! صرت **هامور السيرفر 👑** ومحترف غدر رسمي!"
        elif pts > 200:
            comment = "كفو، مستواك هيبة والكل يحسب حسابك بالديسكورد 😎"
        await ctx.send(f"📊 أهلاً {ctx.author.mention}، نقاطك الحالية هي: **{pts} نقطة**.\n💡 *{comment}*")

    @commands.hybrid_command(name="تصفير_الذيب", description="تصفير كامل نقاط اللعبة (للإدارة فقط)")
    @commands.has_permissions(administrator=True)
    async def reset_cmd(self, ctx: commands.Context):
        await self.db.reset_all()
        await ctx.send("🚨 **تنبيه الإدارة:** تم قش وتصفير قاعدة بيانات نقاط الذئب بالكامل لجميع الأعضاء!")

# -------------------------------------------------------------------
# 3. INTERACTIVE UI COMPONENTS (Views & Dropdowns)
# -------------------------------------------------------------------
class RegistrationView(discord.ui.View):
    def __init__(self, cog, guild_id):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="انضمام للقرية 🐺", style=discord.ButtonStyle.green, custom_id="join_btn")
    async def join(self, interact: discord.Interaction, btn: discord.ui.Button):
        game = self.cog.games.get(self.guild_id)
        if not game or game.phase != "LOBBY":
            return await interact.response.send_message("❌ التسجيل مقفل للقيم هذا، الحين بادي الجلد!", ephemeral=True)
        if interact.user.id in game.players:
            return await interact.response.send_message("❌ مسجل من أول ومسوي زحمة يا بطل!", ephemeral=True)
        game.players.append(interact.user.id)
        
        emb = interact.message.embeds[0]
        emb.set_field_at(0, name="👥 قائمة الضحايا (المسجلين حالياً)", value=", ".join([f"<@{p}>" for p in game.players]), inline=False)
        await interact.message.edit(embed=emb)
        await interact.response.send_message("✅ تم تسجيل دخولك بنجاح للقرية! جهز وضعية الجحود والإنكار.", ephemeral=True)

    @discord.ui.button(label="انسحاب 🏃‍♂️", style=discord.ButtonStyle.danger, custom_id="leave_btn")
    async def leave(self, interact: discord.Interaction, btn: discord.ui.Button):
        game = self.cog.games.get(self.guild_id)
        if not game or interact.user.id not in game.players:
            return await interact.response.send_message("❌ أنت مو مسجل أصلاً عشان تنحاش من الذيابة!", ephemeral=True)
        game.players.remove(interact.user.id)
        emb = interact.message.embeds[0]
        val = ", ".join([f"<@{p}>" for p in game.players]) if game.players else "الروم فاضي.. الكل خايف؟ 🤔"
        emb.set_field_at(0, name="👥 قائمة الضحايا (المسجلين حالياً)", value=val, inline=False)
        await interact.message.edit(embed=emb)
        await interact.response.send_message("🏃‍♂️ تم سحب ملفك بنجاح، انحش بديرتك أزين لك!", ephemeral=True)

    @discord.ui.button(label="شرح الأدوار 📚", style=discord.ButtonStyle.secondary, custom_id="help_btn")
    async def help(self, interact: discord.Interaction, btn: discord.ui.Button):
        text = "📌 **دليل أدوار قرية الفخامة والغدر:**\n\n" + "\n".join([f"**{r.name}**: {r.desc}" for r in ROLES_DATA.values()])
        await interact.response.send_message(text, ephemeral=True)

class RevealRoleView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    @discord.ui.button(label="اكشف هوّيتك السرية بالخفاء 🕵️‍♂️", style=discord.ButtonStyle.blurple, custom_id="reveal_btn")
    async def reveal(self, interact: discord.Interaction, btn: discord.ui.Button):
        if interact.user.id not in self.game.players:
            return await interact.response.send_message("❌ أنت متفرج بس، خلك عاقل وشوف المصخرة من بعيد!", ephemeral=True)
        role = self.game.roles[interact.user.id]
        await interact.response.send_message(f"🤫 **بطاقة دورك السري المنحوت:**\nأنت: **{role.name}**\nالمهمة: {role.desc}", ephemeral=True)

class NightActionPanel(discord.ui.View):
    def __init__(self, cog, game):
        super().__init__(timeout=60)
        self.cog = cog
        self.game = game

    @discord.ui.button(label="تنفيذ التحركات الليلية المخفية 🌌", style=discord.ButtonStyle.success, custom_id="night_action_btn")
    async def execute_action(self, interact: discord.Interaction, btn: discord.ui.Button):
        p_id = interact.user.id
        if p_id not in self.game.alive:
            return await interact.response.send_message("❌ أنت ميت ومنتهي، نم نومة أهل الكهف ولا تقروشنا!", ephemeral=True)
            
        role_name = self.game.roles[p_id].name
        options = [discord.SelectOption(label=interact.guild.get_member(p).display_name, value=str(p)) for p in self.game.alive if p != p_id]
        if not options:
            return await interact.response.send_message("❌ ما فيه أحد متبقي بالقرية تختاره يا كابتن!", ephemeral=True)

        if "الذيب" in role_name:
            view = discord.ui.View()
            sel = discord.ui.Select(placeholder="اختر الفريسة اللي بنعشّ عليها الليلة 🐺", options=options)
            async def call(i: discord.Interaction):
                self.game.night_actions["kill"] = int(sel.values[0])
                await i.response.send_message("🩸 تم تحديد الضحية وغدرها.. جهزوا السكاكين الصباح!", ephemeral=True)
            sel.callback = call
            view.add_item(sel)
            await interact.response.send_message("أنت ذيب خاين، حدد ضحيتك:", view=view, ephemeral=True)
            
        elif "الطبيب" in role_name:
            view = discord.ui.View()
            sel = discord.ui.Select(placeholder="اختر لاعب لتعالجه وتنقذه ⚕️", options=options)
            async def call(i: discord.Interaction):
                self.game.night_actions["heal"] = int(sel.values[0])
                await i.response.send_message("⚕️ تم إعطاء الإبرة للاعب المختار وأموره بالسليم!", ephemeral=True)
            sel.callback = call
            view.add_item(sel)
            await interact.response.send_message("أنت الطبيب، اختر من تبيه يعيش للصباح:", view=view, ephemeral=True)

        elif "المحقق" in role_name:
            if p_id in self.game.used_powers:
                return await interact.response.send_message("❌ استخدمت رادارك وجبت العيد من قبل، خلاص مرة وحدة بس!", ephemeral=True)
            view = discord.ui.View()
            sel = discord.ui.Select(placeholder="اختر لاعب لكشف ملفه الأمني 🔍", options=options)
            async def call(i: discord.Interaction):
                t = int(sel.values[0])
                self.game.used_powers.add(p_id)
                team_ar = "ذيب خاين وراعي غدر 🐺" if self.game.roles[t].team == "wolf" else "قروي غلبان وطيب 🧑‍🌾"
                await i.response.send_message(f"🔍 نتيجة التحقيق السري: اللاعب طلع **{team_ar}**", ephemeral=True)
            sel.callback = call
            view.add_item(sel)
            await interact.response.send_message("أنت المحقق، من تبي تفحص هويته؟", view=view, ephemeral=True)

        elif "الحارس" in role_name:
            if p_id in self.game.used_powers:
                return await interact.response.send_message("❌ استخدمت درع الحماية وخلصت حمايتك يا فزعة!", ephemeral=True)
            view = discord.ui.View()
            sel = discord.ui.Select(placeholder="اختر لاعب لتدعيس الحماية عليه 🛡️", options=options)
            async def call(i: discord.Interaction):
                self.game.night_actions["protect"] = int(sel.values[0])
                self.game.used_powers.add(p_id)
                await i.response.send_message("🛡️ تم تفعيل البلوك والدرع الفولاذي على اللاعب بنجاح!", ephemeral=True)
            sel.callback = call
            view.add_item(sel)
            await interact.response.send_message("أنت الحارس، ضع درعك فوق بطل الليلة:", view=view, ephemeral=True)

        elif "المغرية" in role_name:
            view = discord.ui.View()
            sel = discord.ui.Select(placeholder="اختر لاعب لزيارته وقفل طاقته 💃", options=options)
            async def call(i: discord.Interaction):
                self.game.night_actions["block"] = int(sel.values[0])
                await i.response.send_message("💃 عطلنا طاقته وقفلنا تحركاته بنجاح الليلة!", ephemeral=True)
            sel.callback = call
            view.add_item(sel)
            await interact.response.send_message("أنت المغرية، من ضحيتك لليلة؟", view=view, ephemeral=True)
            
        else:
            await interact.response.send_message("💤 قروي طيب ما عندك أكشن بالليل، ارقد وحط راسك لين يطلع الفرج الصباح!", ephemeral=True)

class DayVotingView(discord.ui.View):
    def __init__(self, cog, game):
        super().__init__(timeout=45)
        self.cog = cog
        self.game = game
        
        options = [discord.SelectOption(label=cog.bot.get_user(p).display_name, value=str(p)) for p in game.alive]
        self.vote_select = discord.ui.Select(placeholder="🗳️ ارمِ صوتك ضد المتهم المريب الحين وفكنا!", options=options)
        self.vote_select.callback = self.vote_callback
        self.add_item(self.vote_select)

    async def vote_callback(self, interact: discord.Interaction):
        p_id = interact.user.id
        if p_id not in self.game.alive:
            return await interact.response.send_message("❌ الأموات يشاهدون بصمت، لا تصوت!", ephemeral=True)
        if p_id in self.game.has_voted:
            return await interact.response.send_message("❌ صوتك مسجل بالصندوق، لا تسوي تزوير انتخابات!", ephemeral=True)
            
        target = int(self.vote_select.values[0])
        weight = 2 if "العمدة" in self.game.roles[p_id].name else 1
        
        self.game.votes[target] = self.game.votes.get(target, 0) + weight
        self.game.has_voted.add(p_id)
        
        await interact.channel.send(f"🗳️ اللاعب {interact.user.mention} صوّت ضد متهم مريب! (ثقل الصوت: {weight})")
        await interact.response.send_message("✅ تم قبول وثيقة صوتك!", ephemeral=True)

    @discord.ui.button(label="👑 مرسوم ملكي إقصائي (للملك فقط)", style=discord.ButtonStyle.danger, row=1)
    async def king_power(self, interact: discord.Interaction, btn: discord.ui.Button):
        p_id = interact.user.id
        if "الملك" not in self.game.roles.get(p_id, Role("", "", "")).name:
            return await interact.response.send_message("❌ منت الملك، لا تسوي فيها هيبة وسلطة هههههه وخّر وخّر!", ephemeral=True)
        if p_id in self.game.used_powers:
            return await interact.response.send_message("❌ استخدمت مرسومك الإقصائي وطيرت راس من قبل يا ملكنا!", ephemeral=True)
            
        options = [discord.SelectOption(label=interact.guild.get_member(p).display_name, value=str(p)) for p in self.game.alive if p != p_id]
        view = discord.ui.View()
        sel = discord.ui.Select(placeholder="اختر الخائن ليتم طيرانه فوراً بأمر ملكي 👑", options=options)
        
        async def call(i: discord.Interaction):
            t = int(sel.values[0])
            self.game.used_powers.add(p_id)
            self.game.king_executed = True
            if t in self.game.alive: self.game.alive.remove(t)
            
            await i.channel.send(f"👑 **مرسوم ملكي قاطع:** قرر جلالة الملك {interact.user.mention} استخدام سلطته وطحن المتهم {i.guild.get_member(t).mention} فوراً بدون محاكمة ولا فلسفة! هويته كانت: **{self.game.roles[t].name}**")
            self.stop()
            if not self.cog.check_win(i, self.game):
                await self.cog.run_night_phase(i, self.game)
                
        sel.callback = call
        view.add_item(sel)
        await interact.response.send_message("مولاي الملك، من تبي تطير رأسه على المقصلة فورا؟", view=view, ephemeral=True)

# -------------------------------------------------------------------
# 4. ROBUST TICKETING SYSTEM COG
# -------------------------------------------------------------------
class TicketSystemCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="تجهيز_التيكت", description="تجهيز لوحة انطلاق التذاكر الفخمة للسيرفر")
    @commands.has_permissions(administrator=True)
    async def setup_ticket(self, ctx: commands.Context):
        embed = discord.Embed(
            title="🎫 نظام تذاكر الدعم الفني والمبيعات المتطور 🎫",
            description="إذا كان عندك أي مشكلة، استفسار، أو تبي تشتري من المتجر والخدمات، اضغط على الزر بالأسفل لفتح تذكرة خاصة بك فوراً وسيتواصل معك المشرفين.",
            color=0x2ecc71
        )
        embed.set_footer(text="سيتم إنشاء روم مشفر خاص بك ومخفي تماماً.")
        await ctx.send(embed=embed, view=TicketHubView())

class TicketHubView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="فتح تذكرة جديدة 🎫", style=discord.ButtonStyle.primary, custom_id="open_ticket_btn")
    async def make_ticket(self, interact: discord.Interaction, btn: discord.ui.Button):
        category = discord.utils.get(interact.guild.categories, name="📌 تذاكر الدعم")
        if not category:
            category = await interact.guild.create_category("📌 تذاكر الدعم")
            
        t_num = random.randint(1000, 9999)
        ch_name = f"ticket-{interact.user.name}-{t_num}"
        
        overwrites = {
            interact.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interact.user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            interact.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        
        ticket_ch = await category.create_text_channel(name=ch_name, overwrites=overwrites)
        
        emb = discord.Embed(
            title=f"مرحباً بك في تذكرتك الخاصة #{t_num}",
            description=f"يا هلا يا {interact.user.mention}! طاقم المبيعات والدعم الفني تم إشعارهم وسيتم الرد عليك فوراً.\nاستخدم لوحة التحكم بالأسفل للتحكم بالتذكرة.",
            color=0x3498db
        )
        await ticket_ch.send(content=f"{interact.user.mention} | الدعم والتحكم", embed=emb, view=TicketControlView(ticket_ch.id, interact.user.id))
        await interact.response.send_message(f"✅ تم فتح تذكرتك السرية بنجاح هنا: {ticket_ch.mention}", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self, ch_id, creator_id):
        super().__init__(timeout=None)
        self.ch_id = ch_id
        self.creator_id = creator_id

    @discord.ui.button(label="قفل التذكرة 🔒", style=discord.ButtonStyle.secondary, custom_id="lock_ticket_btn")
    async def lock_ch(self, interact: discord.Interaction, btn: discord.ui.Button):
        ch = interact.channel
        creator = interact.guild.get_member(self.creator_id)
        if creator:
            await ch.set_permissions(creator, send_messages=False, read_messages=True)
        await interact.response.send_message("🔒 **إشعار الإغلاق:** تم سحب صلاحية الكتابة من العضو بنجاح.")

    @discord.ui.button(label="أرشفة المحادثة 📝", style=discord.ButtonStyle.blurple, custom_id="transcript_ticket_btn")
    async def save_transcript(self, interact: discord.Interaction, btn: discord.ui.Button):
        await interact.response.defer()
        log_text = f"📜 أرشيف تذكرة سيرفر فالي: {interact.channel.name}\nتاريخ الأرشفة: {datetime.now()}\n\n"
        async check_messages = interact.channel.history(limit=1000, oldest_first=True)
        async for m in check_messages:
            log_text += f"[{m.created_at.strftime('%Y-%m-%d %H:%M')}] {m.author.name}: {m.content}\n"
            
        filename = f"transcript-{interact.channel.name}.txt"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(log_text)
            
        await interact.followup.send(file=discord.File(filename))
        os.remove(filename)

    @discord.ui.button(label="حذف الروم نهائياً 🗑️", style=discord.ButtonStyle.danger, custom_id="delete_ticket_btn")
    async def delete_ch(self, interact: discord.Interaction, btn: discord.ui.Button):
        await interact.response.send_message("⚠️ جاري تدمير وحذف التذكرة من السيرفر نهائياً خلال 5 ثوانٍ...")
        await asyncio.sleep(5)
        await interact.channel.delete()

# -------------------------------------------------------------------
# 5. PREMIUM MAIN BOT INITIALIZATION
# -------------------------------------------------------------------
class PremiumCoreBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database()

    async def setup_hook(self):
        await self.db.init()
        await self.add_cog(WerewolfCog(self, self.db))
        await self.add_cog(TicketSystemCog(self))
        
        # Keep persistent views alive across Render restarts!
        self.add_view(TicketHubView())
        
        # Force Slash commands sync with discord on boot!
        print("🔄 جاري مزامنة شجرة الأوامر (Slash Commands) مع ديسكورد...")
        await self.tree.sync()

    async def on_ready(self):
        print(f"==========================================")
        print(f"👑 {self.user.name} الـمـشـروع جـاهـز لـلـقـمـة!")
        print(f"🤖 البوت شغال رسمي وعلى سيرفرات ريندر مستقر!")
        print(f"==========================================")

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("🚨 خطأ فادح: لم يتم العثور على متغير البيئة DISCORD_TOKEN في ريندر!")
    bot = PremiumCoreBot()
    bot.run(token)