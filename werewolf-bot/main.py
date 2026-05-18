import os
import random
import asyncio
import threading
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Select, Button
from flask import Flask

from game import Game, Role, Phase

NIGHT_TIMEOUT = 90
VOTE_TIMEOUT = 120
KING_TIMEOUT = 30

flask_app = Flask("")

@flask_app.route("/")
def home():
    return "\U0001f43a Werewolf Bot is Alive!"

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
games: Dict[int, Game] = {}


async def send_dm(user_id: int, content: str, view: Optional[View] = None) -> bool:
    try:
        user = await bot.fetch_user(user_id)
        await user.send(content, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        return False


class LobbyView(View):
    def __init__(self, game: Game, host_id: int, host_name: str):
        super().__init__(timeout=300)
        self.game = game
        self.host_id = host_id
        self.host_name = host_name

    @discord.ui.button(label="Join", emoji="\u2705", style=discord.ButtonStyle.green)
    async def join_btn(self, interaction: discord.Interaction, button: Button):
        uid = interaction.user.id
        if uid in self.game.players:
            await interaction.response.send_message("You are already in the game!", ephemeral=True)
            return
        if len(self.game.players) >= Game.MAX_PLAYERS:
            await interaction.response.send_message("Game is full!", ephemeral=True)
            return
        self.game.add_player(uid, interaction.user.display_name)
        await interaction.response.defer(ephemeral=True)
        await interaction.message.edit(embed=self._build_embed(), view=self)
        await interaction.followup.send("Joined the game!", ephemeral=True)

    @discord.ui.button(label="Leave", emoji="\u274c", style=discord.ButtonStyle.red)
    async def leave_btn(self, interaction: discord.Interaction, button: Button):
        uid = interaction.user.id
        if uid not in self.game.players:
            await interaction.response.send_message("You are not in the game!", ephemeral=True)
            return
        self.game.remove_player(uid)
        await interaction.response.defer(ephemeral=True)
        await interaction.message.edit(embed=self._build_embed(), view=self)
        await interaction.followup.send("Left the game!", ephemeral=True)

    @discord.ui.button(label="Start Game", emoji="\U0001f3ae", style=discord.ButtonStyle.blurple)
    async def start_btn(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.host_id:
            await interaction.response.send_message("Only the host can start the game!", ephemeral=True)
            return
        if len(self.game.players) < Game.MIN_PLAYERS:
            await interaction.response.send_message(
                f"Need at least {Game.MIN_PLAYERS} players! ({len(self.game.players)} joined)", ephemeral=True
            )
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.channel.send("\U0001f3ae **Game is starting...**")
        await self._launch_game(interaction)

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="\U0001f43a Werewolf Game Lobby",
            description=f"**Players: {self.game.player_count}/{Game.MAX_PLAYERS}**\nMinimum: {Game.MIN_PLAYERS}",
            color=discord.Color.blue(),
        )
        if self.game.players:
            lines = "\n".join(f"{i+1}. {p.name}" for i, p in enumerate(self.game.players.values()))
            e.add_field(name="Joined Players", value=lines, inline=False)
        else:
            e.add_field(name="Joined Players", value="*No one yet*", inline=False)
        e.set_footer(text=f"Host: {self.host_name}")
        return e

    async def _launch_game(self, interaction: discord.Interaction):
        channel = interaction.channel
        game = self.game
        game.distribute_roles()

        start = discord.Embed(
            title="\U0001f43a The Werewolf Game Has Begun!",
            description=f"**{game.player_count} players** are in the game.\nCheck your DMs for your role!",
            color=discord.Color.dark_green(),
        )
        lines = "\n".join(p.name for p in game.players.values())
        start.add_field(name="Players", value=lines, inline=False)
        await channel.send(embed=start)

        failed_dms = []
        for pid, p in game.players.items():
            ok = await send_dm(pid, f"**\U0001f3ad Your role is: {p.role.display_name}**\n{p.role.description}")
            if not ok:
                failed_dms.append(p.name)
            await asyncio.sleep(0.3)

        if failed_dms:
            await channel.send(
                f"\u26a0\ufe0f Could not DM: {', '.join(failed_dms)}. They may need to enable DMs from server members."
            )

        await channel.send("\U0001f319 **Night falls on the village...**")
        await run_night_phase(game, channel)


class NightActionView(View):
    def __init__(self, game: Game, actor_id: int, action_type: str):
        super().__init__(timeout=NIGHT_TIMEOUT)
        self.game = game
        self.actor_id = actor_id
        self.action_type = action_type
        self.selected_target: Optional[int] = None

        options = []
        for p in game.living_players:
            if p.user_id == actor_id:
                continue
            options.append(discord.SelectOption(label=p.name, value=str(p.user_id), emoji="\U0001f3af"))

        if not options:
            options.append(discord.SelectOption(label="No valid targets", value="none"))

        self.select = Select(
            placeholder="Choose your target...",
            options=options[:25],
            min_values=1,
            max_values=1,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        self.confirm = Button(
            label="Confirm",
            style=discord.ButtonStyle.green,
            disabled=(len(options) != 1 or options[0].value == "none"),
            row=1,
        )
        self.confirm.callback = self._on_confirm
        self.add_item(self.confirm)

        if len(options) == 1 and options[0].value != "none":
            self.selected_target = int(options[0].value)
            self.confirm.disabled = False

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This is not your action!", ephemeral=True)
            return
        val = self.select.values[0]
        if val == "none":
            await interaction.response.send_message("No valid targets available.", ephemeral=True)
            return
        self.selected_target = int(val)
        target_name = self.game.players[self.selected_target].name
        self.confirm.disabled = False
        await interaction.response.edit_message(content=f"Selected: **{target_name}**", view=self)

    async def _on_confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This is not your action!", ephemeral=True)
            return
        if self.selected_target is None:
            await interaction.response.send_message("No target selected.", ephemeral=True)
            return

        if self.action_type == "wolf" and self.game.wolf_target is not None:
            await interaction.response.send_message(
                "\U0001f43a Another wolf has already chosen the target tonight!", ephemeral=True
            )
            return

        ok = self.game.set_night_action(self.actor_id, self.selected_target)
        if not ok:
            await interaction.response.send_message("Could not record your action.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True

        if self.action_type == "wolf":
            other_wolves = [p for p in self.game.living_wolves if p.user_id != self.actor_id]
            msg = f"\u2705 Wolf target locked in!"
            if other_wolves:
                msg += " Other wolves have been notified."
            await self._notify_other_wolves(other_wolves)
        else:
            msg = "\u2705 Action recorded! Waiting for others..."

        self.game.completed_actions += 1
        await interaction.response.edit_message(content=msg, view=self)

        if self.game.completed_actions >= self.game.expected_actions:
            self.game.night_complete.set()

    async def _notify_other_wolves(self, other_wolves):
        if not other_wolves:
            return
        target_name = self.game.players[self.game.wolf_target].name
        for w in other_wolves:
            await send_dm(w.user_id, f"\U0001f43a Your pack has chosen **{target_name}** as tonight's target.")


class DayVoteView(View):
    def __init__(self, game: Game):
        super().__init__(timeout=VOTE_TIMEOUT)
        self.game = game
        self.voters: set = set()

        options = [
            discord.SelectOption(label=p.name, value=str(p.user_id), emoji="\U0001f5f3\ufe0f")
            for p in game.living_players
        ]
        if not options:
            options.append(discord.SelectOption(label="No one to vote for", value="none"))

        self.select = Select(
            placeholder="Vote for who to exile...",
            options=options[:25],
            min_values=1,
            max_values=1,
        )
        self.select.callback = self._on_vote
        self.add_item(self.select)

    async def _on_vote(self, interaction: discord.Interaction):
        uid = interaction.user.id
        if uid not in self.game.players:
            await interaction.response.send_message("You are not in this game!", ephemeral=True)
            return
        if not self.game.players[uid].alive:
            await interaction.response.send_message("Dead players cannot vote!", ephemeral=True)
            return
        val = self.select.values[0]
        if val == "none":
            await interaction.response.send_message("No valid target.", ephemeral=True)
            return
        tid = int(val)
        self.game.cast_vote(uid, tid)
        target_name = self.game.players[tid].name
        await interaction.response.send_message(f"\U0001f5f3\ufe0f You voted for **{target_name}**!", ephemeral=True)

        self.voters.add(uid)
        if len(self.voters) >= len(self.game.living_players):
            self.game.vote_complete.set()


class KingActionView(View):
    def __init__(self, game: Game, channel_id: int, king_id: int):
        super().__init__(timeout=KING_TIMEOUT)
        self.game = game
        self.channel_id = channel_id
        self.king_id = king_id
        self.selected_target: Optional[int] = None

        options = [
            discord.SelectOption(label=p.name, value=str(p.user_id), emoji="\U0001f451")
            for p in game.living_players
        ]
        if not options:
            options.append(discord.SelectOption(label="No one", value="none"))

        self.select = Select(
            placeholder="Choose someone to exile...",
            options=options[:25],
            min_values=1,
            max_values=1,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        self.confirm = Button(
            label="Exile!",
            style=discord.ButtonStyle.danger,
            disabled=(len(options) == 1 and options[0].value == "none"),
            row=1,
        )
        self.confirm.callback = self._on_confirm
        self.add_item(self.confirm)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.king_id:
            await interaction.response.send_message("Only the King can use this!", ephemeral=True)
            return
        val = self.select.values[0]
        if val == "none":
            await interaction.response.send_message("No valid targets.", ephemeral=True)
            return
        self.selected_target = int(val)
        self.confirm.disabled = False
        target_name = self.game.players[self.selected_target].name
        await interaction.response.edit_message(content=f"Selected: **{target_name}**", view=self)

    async def _on_confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.king_id:
            return
        if self.selected_target is None:
            await interaction.response.send_message("No target selected.", ephemeral=True)
            return

        target = self.game.players[self.selected_target]
        target.alive = False
        self.game.king_exiled_this_day = True
        self.game.king_exile_target = self.selected_target
        self.game.players[self.king_id].has_used_power = True

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="\u2705 Royal decree issued!", view=self)

        channel = bot.get_channel(self.channel_id)
        if channel:
            await channel.send(
                f"\U0001f451 **The King has spoken!** "
                f"{target.name} ({target.role.display_name}) has been exiled by royal decree!"
            )

        if hasattr(self.game, "king_event"):
            self.game.king_event.set()


async def get_night_actors(game: Game) -> tuple:
    wolves = []
    others = []
    for p in game.living_players:
        r = p.role
        if r == Role.WOLF:
            wolves.append(("wolf", p))
        elif r == Role.DOCTOR:
            others.append(("doctor", p))
        elif r == Role.SEDUCER:
            others.append(("seducer", p))
        elif r == Role.DETECTIVE and not p.has_used_power:
            others.append(("detective", p))
        elif r == Role.BODYGUARD and not p.has_used_power:
            others.append(("bodyguard", p))
    return wolves + others  # All wolves included


async def run_night_phase(game: Game, channel: discord.TextChannel):
    game.start_night()
    game.night_complete = asyncio.Event()
    game.completed_actions = 0

    night_embed = discord.Embed(
        title=f"\U0001f319 Night {game.night_number}",
        description="The village sleeps... The wolves are on the prowl!",
        color=discord.Color.dark_blue(),
    )
    await channel.send(embed=night_embed)

    actors = await get_night_actors(game)
    wolf_count = len([a for a in actors if a[0] == "wolf"])
    non_wolf_count = len(actors) - wolf_count
    game.expected_actions = non_wolf_count + (1 if wolf_count > 0 else 0)

    if actors:
        for action_type, player in actors:
            view = NightActionView(game, player.user_id, action_type)
            role_emoji = player.role.emoji if player.role else ""
            ok = await send_dm(
                player.user_id,
                f"\U0001f319 **Night {game.night_number} - Your turn!**\n{role_emoji} You are **{player.role.display_name}**\nChoose your target:",
                view=view,
            )
            if not ok:
                await channel.send(f"\u26a0\ufe0f Could not DM {player.name} for their night action!")

            if action_type == "wolf":
                other_wolves = [p for p in game.living_wolves if p.user_id != player.user_id]
                if other_wolves:
                    names = ", ".join(p.name for p in other_wolves)
                    await send_dm(player.user_id, f"\U0001f43a **Your pack members:** {names}")

            await asyncio.sleep(0.5)

        try:
            await asyncio.wait_for(game.night_complete.wait(), timeout=NIGHT_TIMEOUT)
        except asyncio.TimeoutError:
            pass
    else:
        game.night_complete.set()

    results = game.resolve_night()

    dawn = discord.Embed(title="\u2600\ufe0f Dawn", color=discord.Color.gold())
    if results["messages"]:
        for msg in results["messages"]:
            dawn.add_field(name="\u200b", value=msg, inline=False)
    else:
        dawn.description = random.choice(Game.NO_DEATH_MESSAGES)

    await channel.send(embed=dawn)

    if results["detective_result"] is not None and results["detective_target_name"]:
        for p in game.living_players:
            if p.role == Role.DETECTIVE:
                verdict = "\U0001f43a A Wolf" if results["detective_result"] else "\U0001f9d1\U0001f33e Not a Wolf"
                await send_dm(p.user_id, f"\U0001f50d **Investigation Result:** {results['detective_target_name']} is **{verdict}**!")
                break

    om_id = results.get("om_zaki_reveal")
    if om_id:
        om_name = game.players[om_id].name
        sarcastic = random.choice([
            f"\U0001f475 **Om Zaki:** 'Oh look, {om_name} is a WOLF! And here I thought you were just ugly!'",
            f"\U0001f475 **Om Zaki's dying curse:** '{om_name} is a wolf! I knew it! Your mother was a hamster!'",
            f"\U0001f475 **Om Zaki exposes {om_name} as a Wolf!** 'I would say I'm surprised, but I'm not.'",
            f"\U0001f475 **Om Zaki's last words:** 'The wolf is {om_name}! ...Also, tell my cat I love her.'",
            f"\U0001f475 **Om Zaki:** '{om_name} is a WOLF? Well, that explains the excessive howling at the moon.'",
        ])
        await channel.send(sarcastic)

    winner = game.check_winner()
    if winner:
        await end_game(game, channel, winner)
        return

    await run_day_phase(game, channel)


async def run_day_phase(game: Game, channel: discord.TextChannel):
    game.start_day()

    day_embed = discord.Embed(
        title=f"\u2600\ufe0f Day {game.day_number}",
        description="The villagers discuss and decide who to exile!",
        color=discord.Color.gold(),
    )
    living_list = "\n".join(f"\U0001f7e2 {p.name}" for p in game.living_players)
    day_embed.add_field(name="Living Players", value=living_list, inline=False)
    await channel.send(embed=day_embed)

    await asyncio.sleep(3)

    king = None
    for p in game.living_players:
        if p.role == Role.KING and not p.has_used_power:
            king = p
            break

    if king:
        await channel.send("\U0001f451 **The King** may use their royal power to instantly exile someone...")
        king_event = asyncio.Event()
        game.king_event = king_event

        view = KingActionView(game, channel.id, king.user_id)
        ok = await send_dm(
            king.user_id,
            "\U0001f451 **Your Royal Power awaits!** Choose someone to exile instantly, overriding all votes!",
            view=view,
        )
        if not ok:
            await channel.send(f"\u26a0\ufe0f Could not DM the King ({king.name})!")

        try:
            await asyncio.wait_for(king_event.wait(), timeout=KING_TIMEOUT)
            if game.king_exiled_this_day:
                winner = game.check_winner()
                if winner:
                    await end_game(game, channel, winner)
                    return
                await run_night_phase(game, channel)
                return
        except asyncio.TimeoutError:
            await channel.send("\U0001f451 The King chose not to use their power.")

    await run_voting_phase(game, channel)


async def run_voting_phase(game: Game, channel: discord.TextChannel):
    game.vote_complete = asyncio.Event()

    vote_embed = discord.Embed(
        title="\U0001f5f3\ufe0f Voting Time!",
        description="Cast your vote using the dropdown below!",
        color=discord.Color.orange(),
    )
    vote_embed.add_field(
        name="Living Players",
        value="\n".join(f"{p.name}" for p in game.living_players),
        inline=False,
    )
    await channel.send(embed=vote_embed, view=DayVoteView(game))

    try:
        await asyncio.wait_for(game.vote_complete.wait(), timeout=VOTE_TIMEOUT)
    except asyncio.TimeoutError:
        pass

    target_id = game.get_most_voted()

    if target_id is None:
        await channel.send("\U0001f5f3\ufe0f **No one was exiled!** Tie vote or no votes cast.")
    else:
        result = game.exile_player(target_id)
        msg = random.choice(Game.EXILE_MESSAGES).format(name=result["name"], role=result["role"])
        await channel.send(f"**{msg}**")

        winner = game.check_winner()
        if winner:
            await end_game(game, channel, winner)
            return

    await run_night_phase(game, channel)


async def end_game(game: Game, channel: discord.TextChannel, winner: str):
    game.phase = Phase.FINISHED

    if winner == "wolves":
        embed = discord.Embed(
            title="\U0001f43a **The Wolves Win!**",
            description="The wolves have taken over the village!",
            color=discord.Color.red(),
        )
    else:
        embed = discord.Embed(
            title="\U0001f9d1\U0001f33e **The Villagers Win!**",
            description="The village is safe again!",
            color=discord.Color.green(),
        )

    role_lines = []
    for p in game.players.values():
        status = "\U0001f480" if not p.alive else "\u2705"
        role_lines.append(f"{status} {p.name} - {p.role.display_name if p.role else 'Unknown'}")
    embed.add_field(name="Final Roles", value="\n".join(role_lines), inline=False)
    await channel.send(embed=embed)

    if game.channel_id in games:
        del games[game.channel_id]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Command sync failed: {e}")


@bot.tree.command(
    name="start_game",
    description="Start a new Werewolf game lobby in this channel",
)
async def start_game(interaction: discord.Interaction):
    cid = interaction.channel_id
    if cid in games:
        await interaction.response.send_message(
            "A game is already running in this channel! Use `/end_game` to force-stop it.",
            ephemeral=True,
        )
        return

    game = Game(cid, interaction.user.id)
    games[cid] = game

    embed = discord.Embed(
        title="\U0001f43a Werewolf Game Lobby",
        description=(
            f"**Players: 0/{Game.MAX_PLAYERS}** (min: {Game.MIN_PLAYERS})\n\n"
            "\u2705 **Join** to enter the game\n"
            "\u274c **Leave** to drop out\n"
            "\U0001f3ae **Start Game** (host only) to begin!"
        ),
        color=discord.Color.blue(),
    )
    embed.set_footer(text=f"Host: {interaction.user.display_name}")

    view = LobbyView(game, interaction.user.id, interaction.user.display_name)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(
    name="end_game",
    description="Force-end the current game in this channel (host only)",
)
async def end_game_cmd(interaction: discord.Interaction):
    cid = interaction.channel_id
    if cid not in games:
        await interaction.response.send_message("No game running in this channel.", ephemeral=True)
        return
    game = games[cid]
    if interaction.user.id != game.host_id:
        await interaction.response.send_message("Only the host can end the game.", ephemeral=True)
        return
    del games[cid]
    game.phase = Phase.FINISHED
    await interaction.response.send_message("Game force-ended.", ephemeral=True)
    await interaction.channel.send("\U0001f3f3\ufe0f **The game has been ended by the host.**")


@bot.tree.command(
    name="players",
    description="Show living players in the current game",
)
async def players_cmd(interaction: discord.Interaction):
    cid = interaction.channel_id
    if cid not in games:
        await interaction.response.send_message("No game running in this channel.", ephemeral=True)
        return
    game = games[cid]
    alive = game.living_players
    if not alive:
        await interaction.response.send_message("No living players.", ephemeral=True)
        return
    lines = "\n".join(f"\U0001f7e2 {p.name}" for p in alive)
    await interaction.response.send_message(f"**Living Players ({len(alive)}):**\n{lines}", ephemeral=True)


try:
    TOKEN = os.environ["MTUwNTg4MzA5NDI5NTc3NzM5MQ.G3g835.QhNEPiQvtnbq1Clergs-liEWWTVpwHFZqIddKs"]
except KeyError:
    TOKEN = input("Enter your Discord bot token: ").strip()

t = threading.Thread(target=run_flask, daemon=True)
t.start()

bot.run(TOKEN)
