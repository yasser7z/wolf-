import random
from enum import Enum
from typing import Optional, List, Dict


class Phase(Enum):
    LOBBY = "lobby"
    NIGHT = "night"
    DAY = "day"
    FINISHED = "finished"


class Role(Enum):
    VILLAGER = "villager"
    WOLF = "wolf"
    DETECTIVE = "detective"
    BODYGUARD = "bodyguard"
    KING = "king"
    MAYOR = "mayor"
    DOCTOR = "doctor"
    SEDUCER = "seducer"
    OM_ZAKI = "om_zaki"

    @property
    def emoji(self) -> str:
        return {
            Role.VILLAGER: "\U0001f9d1\U0001f33e",
            Role.WOLF: "\U0001f43a",
            Role.DETECTIVE: "\U0001f50d",
            Role.BODYGUARD: "\U0001f6e1\ufe0f",
            Role.KING: "\U0001f451",
            Role.MAYOR: "\U0001f3db\ufe0f",
            Role.DOCTOR: "\u2695\ufe0f",
            Role.SEDUCER: "\U0001f483",
            Role.OM_ZAKI: "\U0001f475",
        }[self]

    @property
    def display_name(self) -> str:
        names = {
            Role.VILLAGER: "Villager (\u0627\u0644\u0642\u0631\u0648\u064a)",
            Role.WOLF: "Wolf (\u0627\u0644\u0630\u064a\u0628)",
            Role.DETECTIVE: "Detective (\u0627\u0644\u0645\u062d\u0642\u0642)",
            Role.BODYGUARD: "Bodyguard (\u0627\u0644\u062d\u0627\u0631\u0633)",
            Role.KING: "King (\u0627\u0644\u0645\u0644\u0643)",
            Role.MAYOR: "Mayor (\u0627\u0644\u0639\u0645\u062f\u0629)",
            Role.DOCTOR: "Doctor (\u0627\u0644\u0637\u0628\u064a\u0628)",
            Role.SEDUCER: "Seducer (\u0627\u0644\u0645\u063a\u0631\u064a\u0629)",
            Role.OM_ZAKI: "Om Zaki (\u0623\u0645 \u0632\u0643\u064a)",
        }
        return f"{self.emoji} {names[self]}"

    @property
    def description(self) -> str:
        return {
            Role.VILLAGER: "No special powers. Use your voice and vote to find and exile the wolves!",
            Role.WOLF: "Each night, choose a player to kill. Blend in during the day. Win when Wolves \u2265 Civilians!",
            Role.DETECTIVE: "Once per game, investigate a player to learn if they are a Wolf or not.",
            Role.BODYGUARD: "Once per game, shield a player from the wolves' attack.",
            Role.KING: "Once per game during the day, instantly exile any player, overriding all votes!",
            Role.MAYOR: "Your vote counts as **2 votes** in every daily tally!",
            Role.DOCTOR: "Every night, heal one player. If wolves target them, they survive!",
            Role.SEDUCER: "Visit a player each night. If they are a Wolf, both die. If wolves attack your target, you save them!",
            Role.OM_ZAKI: "If wolves kill you, one random wolf will be exposed publicly!",
        }[self]

    @property
    def is_wolf(self) -> bool:
        return self == Role.WOLF

    @property
    def is_special(self) -> bool:
        return self not in (Role.VILLAGER, Role.WOLF)

    @property
    def can_act_at_night(self) -> bool:
        return self in (Role.WOLF, Role.DETECTIVE, Role.BODYGUARD, Role.DOCTOR, Role.SEDUCER)

    @property
    def uses_once(self) -> bool:
        return self in (Role.DETECTIVE, Role.BODYGUARD, Role.KING)

    @property
    def can_act_every_night(self) -> bool:
        return self in (Role.WOLF, Role.DOCTOR, Role.SEDUCER)


class Player:
    def __init__(self, user_id: int, name: str):
        self.user_id = user_id
        self.name = name
        self.role: Optional[Role] = None
        self.alive = True
        self.vote_target: Optional[int] = None
        self.night_action_target: Optional[int] = None
        self.has_used_power = False

    def reset_night(self):
        self.night_action_target = None

    def reset_vote(self):
        self.vote_target = None


class Game:
    MAX_PLAYERS = 20
    MIN_PLAYERS = 4

    ROLE_PRIORITY = [
        Role.DETECTIVE,
        Role.DOCTOR,
        Role.BODYGUARD,
        Role.KING,
        Role.MAYOR,
        Role.SEDUCER,
        Role.OM_ZAKI,
    ]

    DEATH_MESSAGES = [
        "\U0001f43a The wolves had a delicious dinner! **{name}** was found dead this morning.",
        "\U0001f319 **{name}** didn't make it through the night. The wolves strike again!",
        "\U0001f480 RIP **{name}**. The wolves were hungry last night.",
        "\U0001f575\ufe0f The wolves claimed another victim: **{name}**. Will justice be served?",
        "\U0001f43a Nom nom nom! **{name}** was the wolves' midnight snack.",
    ]

    NO_DEATH_MESSAGES = [
        "\u2600\ufe0f The village wakes up to find everyone alive! The wolves were ineffective.",
        "\U0001f305 Another day dawns and everyone is safe. The wolves failed!",
        "\U0001f3e1 No one died last night! Perhaps the wolves are playing nice?",
    ]

    EXILE_MESSAGES = [
        "\U0001f3db\ufe0f The village has spoken! **{name}** ({role}) has been exiled!",
        "\u2696\ufe0f Justice is served! **{name}** ({role}) is banished from the village!",
        "\U0001f6b6 **{name}** ({role}) walks the plank! The village has decided!",
    ]

    OM_ZAKI_REVEALS = [
        "\U0001f475 **Om Zaki:** 'Oh look, {name} is a WOLF! And here I thought you were just ugly!'",
        "\U0001f475 **Om Zaki's dying curse:** '{name} is a wolf! I knew it! Your mother was a hamster!'",
        "\U0001f475 **Om Zaki exposes {name} as a Wolf!** 'I would say I'm surprised, but I'm not.'",
        "\U0001f475 **Om Zaki's last words:** 'The wolf is {name}! ...Also, tell my cat I love her.'",
        "\U0001f475 **Om Zaki:** '{name} is a WOLF? Well, that explains the excessive howling at the moon.'",
    ]

    def __init__(self, channel_id: int, host_id: int):
        self.channel_id = channel_id
        self.host_id = host_id
        self.players: Dict[int, Player] = {}
        self.phase: Phase = Phase.LOBBY
        self.day_number = 0
        self.night_number = 0
        self.wolf_target: Optional[int] = None
        self.king_exiled_this_day = False
        self.king_exile_target: Optional[int] = None

    def add_player(self, user_id: int, name: str) -> bool:
        if len(self.players) >= self.MAX_PLAYERS:
            return False
        if user_id in self.players:
            return False
        self.players[user_id] = Player(user_id, name)
        return True

    def remove_player(self, user_id: int) -> bool:
        if user_id in self.players:
            del self.players[user_id]
            return True
        return False

    @property
    def player_count(self) -> int:
        return len(self.players)

    @property
    def living_players(self) -> List[Player]:
        return [p for p in self.players.values() if p.alive]

    @property
    def living_wolves(self) -> List[Player]:
        return [p for p in self.players.values() if p.alive and p.role == Role.WOLF]

    @property
    def living_civilians(self) -> List[Player]:
        return [p for p in self.players.values() if p.alive and p.role != Role.WOLF]

    @staticmethod
    def calculate_roles(player_count: int) -> List[Role]:
        if player_count < Game.MIN_PLAYERS or player_count > Game.MAX_PLAYERS:
            raise ValueError(f"Player count must be between {Game.MIN_PLAYERS} and {Game.MAX_PLAYERS}")

        if player_count <= 5:
            wolf_count = 1
        elif player_count <= 11:
            wolf_count = 2
        elif player_count <= 16:
            wolf_count = 3
        else:
            wolf_count = 4

        remaining = player_count - wolf_count
        specials_pool = Game.ROLE_PRIORITY[:]

        if player_count <= 5:
            max_specials = min(2, remaining - 1)
        elif player_count <= 7:
            max_specials = min(3, remaining - 1)
        elif player_count <= 10:
            max_specials = min(4, remaining - 1)
        elif player_count <= 14:
            max_specials = min(5, remaining - 1)
        else:
            max_specials = min(7, remaining - 1)

        if remaining - max_specials < 1:
            max_specials = remaining - 1
        if remaining - max_specials < 2 and player_count >= 6:
            max_specials = max(1, remaining - 2)

        selected_specials = specials_pool[:max_specials]
        villager_count = remaining - len(selected_specials)

        roles = [Role.WOLF] * wolf_count + selected_specials + [Role.VILLAGER] * villager_count
        random.shuffle(roles)
        return roles

    def distribute_roles(self) -> Dict[int, Role]:
        roles = self.calculate_roles(len(self.players))
        pids = list(self.players.keys())
        random.shuffle(pids)
        for pid, role in zip(pids, roles):
            self.players[pid].role = role
        return {pid: self.players[pid].role for pid in pids}

    def start_night(self):
        self.phase = Phase.NIGHT
        self.night_number += 1
        self.wolf_target = None
        for p in self.players.values():
            if p.alive:
                p.reset_night()

    def start_day(self):
        self.phase = Phase.DAY
        self.day_number += 1
        self.king_exiled_this_day = False
        self.king_exile_target = None
        for p in self.players.values():
            if p.alive:
                p.reset_vote()

    def set_night_action(self, user_id: int, target_id: int) -> bool:
        if user_id not in self.players:
            return False
        p = self.players[user_id]
        if not p.alive:
            return False
        if target_id not in self.players or not self.players[target_id].alive:
            return False
        if p.role == Role.WOLF:
            self.wolf_target = target_id
            return True
        if p.role in (Role.DETECTIVE, Role.BODYGUARD) and p.has_used_power:
            return False
        p.night_action_target = target_id
        if p.role in (Role.DETECTIVE, Role.BODYGUARD):
            p.has_used_power = True
        return True

    def resolve_night(self) -> dict:
        results = {
            "killed": [],
            "messages": [],
            "om_zaki_reveal": None,
            "detective_result": None,
            "detective_target_name": None,
        }

        wolves = self.living_wolves
        target_id = self.wolf_target

        if not wolves or target_id is None:
            results["messages"].append("\U0001f319 The wolves were restless and didn't kill anyone.")
            self._resolve_detective(results)
            return results

        target = self.players.get(target_id)
        if not target or not target.alive:
            results["messages"].append("\U0001f319 The wolves' target was invalid.")
            self._resolve_detective(results)
            return results

        target_name = target.name
        saved = False
        reason = None

        seducer = self._find_actor(Role.SEDUCER)
        doctor = self._find_actor(Role.DOCTOR)
        bodyguard = self._find_actor(Role.BODYGUARD)

        if seducer:
            seduced_id = seducer.night_action_target
            seduced = self.players.get(seduced_id) if seduced_id else None
            if seduced and seduced.alive:
                if seduced.role == Role.WOLF:
                    seducer.alive = False
                    seduced.alive = False
                    results["killed"].extend([seducer.user_id, seduced.user_id])
                    results["messages"].append(
                        f"\U0001f483 The Seducer visited {seduced.name} and discovered they were a Wolf! Both perished!"
                    )
                elif target_id == seduced_id:
                    saved = True
                    reason = "Seducer"

        if not self.living_wolves:
            saved = True
            reason = "NoWolves"

        if not saved and doctor and doctor.night_action_target == target_id:
            saved = True
            reason = "Doctor"

        if not saved and bodyguard and bodyguard.night_action_target == target_id:
            saved = True
            reason = "Bodyguard"

        if saved:
            savior_msgs = {
                "Seducer": f"\U0001f483 The Seducer was visiting {target_name} and shielded them from the wolves!",
                "Doctor": f"\u2695\ufe0f The Doctor healed {target_name} just in time!",
                "Bodyguard": f"\U0001f6e1\ufe0f The Bodyguard's shield protected {target_name}!",
                "NoWolves": f"\U0001f483 With all wolves dead, {target_name} survives the night!",
            }
            results["messages"].append(savior_msgs.get(reason, f"{target_name} was saved!"))
        else:
            target.alive = False
            results["killed"].append(target_id)

            if target.role == Role.OM_ZAKI:
                lw = self.living_wolves
                if lw:
                    rw = random.choice(lw)
                    results["om_zaki_reveal"] = rw.user_id
                    results["messages"].append(
                        random.choice(Game.OM_ZAKI_REVEALS).format(name=rw.name)
                    )
            else:
                results["messages"].append(
                    random.choice(Game.DEATH_MESSAGES).format(name=target_name)
                )

        self._resolve_detective(results)
        return results

    def _resolve_detective(self, results: dict):
        for p in self.living_players:
            if p.role == Role.DETECTIVE and p.night_action_target is not None:
                inv = self.players.get(p.night_action_target)
                if inv and inv.alive:
                    results["detective_result"] = (inv.role == Role.WOLF)
                    results["detective_target_name"] = inv.name
                break

    def _find_actor(self, role: Role) -> Optional[Player]:
        for p in self.living_players:
            if p.role == role and p.night_action_target is not None:
                return p
        return None

    def cast_vote(self, voter_id: int, target_id: int) -> bool:
        if voter_id not in self.players or target_id not in self.players:
            return False
        voter = self.players[voter_id]
        target = self.players[target_id]
        if not voter.alive or not target.alive:
            return False
        voter.vote_target = target_id
        return True

    def tally_votes(self) -> Dict[int, int]:
        counts: Dict[int, int] = {}
        for p in self.living_players:
            if p.vote_target is not None:
                weight = 2 if p.role == Role.MAYOR else 1
                counts[p.vote_target] = counts.get(p.vote_target, 0) + weight
        return counts

    def get_most_voted(self) -> Optional[int]:
        counts = self.tally_votes()
        if not counts:
            return None
        mv = max(counts.values())
        top = [uid for uid, c in counts.items() if c == mv]
        return top[0] if len(top) == 1 else None

    def exile_player(self, target_id: int) -> dict:
        p = self.players[target_id]
        p.alive = False
        return {"exiled": target_id, "name": p.name, "role": p.role.display_name if p.role else "Unknown"}

    def check_winner(self) -> Optional[str]:
        wolves = self.living_wolves
        civilians = self.living_civilians
        if not wolves:
            return "civilians"
        if len(wolves) >= len(civilians):
            return "wolves"
        return None
