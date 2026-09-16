#!/usr/bin/env python3
"""
meatbot.py — a Discord bot that runs speedrun-style race lobbies.

Flow:
    .make [name]                 -> creates a public #raceroom-<name> channel with a seed
    .makeprivate [name] [@users] -> same, but invite-only; mentioned users get access
    .enter                       -> join the race (in the race channel)
    .ready                       -> mark yourself ready
    .start                       -> admin only; pings anyone not ready, otherwise counts down
    .done                        -> stop your timer
    .forfeit                     -> drop out of a running race
    ...once every entrant is done or forfeited, results are posted.

Lobby channels are created in the same category as the channel the command was
run from.

The person who opened the race is its admin. They don't have to race themselves.

Requires: python 3.10+, discord.py 2.x   (pip install -U "discord.py")
NOTE: prefix commands need the *Message Content* privileged intent, which you
must enable at https://discord.com/developers/applications -> Bot -> Intents.

Run with: DISCORD_TOKEN=... python meatbot.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import re
import string
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from dotenv import load_dotenv

import discord
from discord.ext import commands

load_dotenv()

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

COMMAND_PREFIX = "."
COUNTDOWN_SECONDS = 10            # length of the countdown after .start
COUNTDOWN_TICK_FROM = 3           # start posting big numbers when this many
                                  # seconds remain. Discord allows ~5 messages
                                  # per 5s per channel; above 4 the ticks start
                                  # getting throttled and land late. The live
                                  # <t:..:R> stamp covers the earlier seconds.
MIN_ENTRANTS = 1                  # set to 1 if you want to test solo
CLEANUP_MINUTES = 30              # delete the channel this long after results (0 = never)
ERROR_LINGER = 15                 # seconds before error replies delete themselves (0 = keep)
CHANNEL_PREFIX = "raceroom"       # lobby channels are named <prefix>-<id>
FIGHT_CLUB_INTERMISSION = 60      # seconds between fight club matchups

# Channels where meatbot will accept commands. Entries can be channel IDs (ints,
# via right-click -> Copy Channel ID with Developer Mode on) or names (strings,
# no leading #). An empty set means "anywhere". Race channels the bot created are
# always allowed regardless of what's in here.
ALLOWED_CHANNELS: set[int | str] = {"casual-lobby", "event-lobby"}

# (name, single-letter alias or None, description). Split by where the command
# is usable, so the lobby intro and .help can each show only what applies.

# Usable anywhere except inside a lobby.
GENERAL_COMMANDS = [
    ("make",      None, "Start a public race lobby: `.make aso`"),
    ("makeprivate", None, "Start a private race lobby: `.makeprivate finals @user1 @user2`"),
    ("help",      "h",  "Show this list"),
]

# Usable by anyone inside a lobby.
LOBBY_COMMANDS = [
    ("enter",   "e",  "Join the current race"),
    ("ready",   "r",  "You've entered the seed and are ready to race"),
    ("unready", "u",  "You're not ready anymore (this will halt the countdown)"),
    ("unenter", "u",  "Leave the current race"),
    ("done",    "d",  "You've finished the race"),
    ("forfeit", "f",  "You've given up on finishing the race"),
    ("undone",  "z",  "Undo a `.done` or `.forfeit` and keep racing"),
    ("info",    "i",  "Show the current race status (or just check the pins)"),
    ("help",    "h",  "Show this list"),
]

# Lobby commands restricted to the race admin. `adduser` is private lobbies only.
LOBBY_ADMIN_COMMANDS = [
    ("start",      None, "Starts the countdown, private lobby countdowns don't start automatically"),
    ("stop",       None, "Stops the countdown"),
    ("setseed",    None, "Reroll the seed, or set one explicitly: `.setseed 31415926` (unreadies everyone)"),
    ("adduser",    None, "Add @user to this race lobby"),
    ("removeuser", None, "Remove @user from this race lobby"),
    ("setadmin",   None, "Hand this lobby over to @user"),
    ("new",         None, "After a race ends, start a new race with a fresh seed"),
    ("forcefinish", None, "Force end the current race"),
]


SEED_MIN = 1
SEED_MAX = 4294967295  # 2**32 - 1


def generate_seed() -> int:
    """A uniform seed in [SEED_MIN, SEED_MAX]."""
    return random.randint(SEED_MIN, SEED_MAX)


def random_race_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


MENTION_RE = re.compile(r"<@[!&]?\d+>")


def slugify(raw: str) -> str:
    """Squeeze user input into something Discord will accept as a channel name."""
    slug = re.sub(r"[^a-z0-9]+", "-", MENTION_RE.sub(" ", raw).lower()).strip("-")
    return slug[:24]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class RaceState(Enum):
    OPEN = "open"
    COUNTDOWN = "counting down"
    IN_PROGRESS = "racing"
    FINISHED = "finished"


class EntrantState(Enum):
    ENTERED = "entered"
    READY = "ready"
    DONE = "done"
    FORFEIT = "forfeit"
    UNFINISHED = "unfinished"  # still going when an admin force-finished


@dataclass
class Entrant:
    user_id: int
    state: EntrantState = EntrantState.ENTERED
    finish_time: float | None = None  # elapsed seconds

    @property
    def mention(self) -> str:
        return f"<@{self.user_id}>"


@dataclass
class Race:
    race_id: str                          # also the channel name suffix
    guild_id: int
    channel_id: int
    admin_id: int                         # whoever opened it; need not be an entrant
    seed: int
    race_number: int = 1                  # increments each time .new is run
    private: bool = False
    state: RaceState = RaceState.OPEN
    entrants: dict[int, Entrant] = field(default_factory=dict)
    status_message_id: int | None = None
    start_deadline: float | None = None   # monotonic clock value of "GO"
    started_at: datetime | None = None    # wall clock, for display
    auto_start: bool = True               # False = wait for the admin's .start
    countdown_task: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    # Set when the race ends, so a supervisor (the fight club loop) can wait on it.
    finished_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # -- helpers ----------------------------------------------------------- #

    @property
    def not_ready(self) -> list[Entrant]:
        return [e for e in self.entrants.values() if e.state is not EntrantState.READY]

    @property
    def everyone_finished(self) -> bool:
        return bool(self.entrants) and all(
            e.state in (EntrantState.DONE, EntrantState.FORFEIT)
            for e in self.entrants.values()
        )

    @property
    def finishers(self) -> list[Entrant]:
        done = [e for e in self.entrants.values() if e.state is EntrantState.DONE]
        return sorted(done, key=lambda e: e.finish_time or 0.0)

    def elapsed(self) -> float:
        if self.start_deadline is None:
            return 0.0
        return time.monotonic() - self.start_deadline


# channel_id -> Race
races: dict[int, Race] = {}


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


def format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    hundredths = int((seconds - int(seconds)) * 100)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}.{hundredths:02d}"
    return f"{minutes}:{secs:02d}.{hundredths:02d}"


PLACE_ICONS = {1: "🥇", 2: "🥈", 3: "🥉"}
PLACE_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def entrant_lines(race: Race) -> list[str]:
    lines: list[str] = []
    for place, entrant in enumerate(race.finishers, start=1):
        icon = PLACE_ICONS.get(place, f"`{place}.`")
        lines.append(f"{icon} {entrant.mention} — **{format_time(entrant.finish_time or 0)}**")

    for entrant in race.entrants.values():
        if entrant.state is EntrantState.READY:
            lines.append(f"✅ {entrant.mention} — ready")
        elif entrant.state is EntrantState.ENTERED:
            label = "racing" if race.state is RaceState.IN_PROGRESS else "not ready"
            lines.append(f"⏳ {entrant.mention} — {label}")

    for entrant in race.entrants.values():
        if entrant.state is EntrantState.FORFEIT:
            lines.append(f"🏳️ {entrant.mention} — forfeited")
        elif entrant.state is EntrantState.UNFINISHED:
            lines.append(f"⏹️ {entrant.mention} — unfinished")

    return lines


def format_command(name: str, alias: str | None, description: str) -> str:
    label = f"`{COMMAND_PREFIX}{name}`"
    if alias:
        label += f" / `{COMMAND_PREFIX}{alias}`"
    return f"{label} — {description}"


def build_lobby_commands_embed(private: bool, title: str) -> discord.Embed:
    """The command list shown in a lobby: racer commands plus an admin section."""
    embed = discord.Embed(
        title=title,
        description="\n".join(format_command(*row) for row in LOBBY_COMMANDS),
        colour=discord.Colour.blurple(),
    )
    admin_rows = []
    for name, alias, desc in LOBBY_ADMIN_COMMANDS:
        # Add/remove are meaningless in a lobby everyone can already see.
        if not private and name in ("adduser", "removeuser"):
            continue
        # Public lobbies start on their own, so .start is only a nudge there.
        if not private and name == "start":
            desc = "Nudge anyone who has entered but isn't ready"
        admin_rows.append((name, alias, desc))
    embed.add_field(
        name="Admin only",
        value="\n".join(format_command(*row) for row in admin_rows),
        inline=False,
    )
    return embed


def build_status_embed(race: Race) -> discord.Embed:
    colour = {
        RaceState.OPEN: discord.Colour.blurple(),
        RaceState.COUNTDOWN: discord.Colour.gold(),
        RaceState.IN_PROGRESS: discord.Colour.green(),
        RaceState.FINISHED: discord.Colour.dark_grey(),
    }[race.state]

    embed = discord.Embed(title=f"Race #{race.race_number}", colour=colour)
    embed.add_field(name="Seed", value=f"`{race.seed}`", inline=True)
    embed.add_field(name="Status", value=race.state.value.title(), inline=True)
    embed.add_field(name="Admin", value=f"<@{race.admin_id}>", inline=True)

    lines = entrant_lines(race)
    embed.description = "\n".join(lines) if lines else "*Nobody has entered yet — `.e` to join.*"

    if race.started_at:
        embed.set_footer(text="Started")
        embed.timestamp = race.started_at
    return embed


def build_results_embed(race: Race) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏁 Race #{race.race_number} Results",
        description=f"Seed `{race.seed}`",
        colour=discord.Colour.green(),
    )
    finishers = race.finishers
    if finishers:
        embed.add_field(
            name="Finished",
            value="\n".join(
                f"{PLACE_ICONS.get(i, f'`{i}.`')} {e.mention} — **{format_time(e.finish_time or 0)}**"
                for i, e in enumerate(finishers, start=1)
            ),
            inline=False,
        )
    forfeits = [e for e in race.entrants.values() if e.state is EntrantState.FORFEIT]
    if forfeits:
        embed.add_field(
            name="Did not finish",
            value="\n".join(f"🏳️ {e.mention}" for e in forfeits),
            inline=False,
        )
    unfinished = [e for e in race.entrants.values() if e.state is EntrantState.UNFINISHED]
    if unfinished:
        embed.add_field(
            name="Unfinished",
            value="\n".join(f"⏹️ {e.mention} — still running when the race was ended"
                            for e in unfinished),
            inline=False,
        )
    if not finishers and not forfeits and not unfinished:
        embed.add_field(name="Finished", value="*Nobody.*", inline=False)
    return embed


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #

intents = discord.Intents.default()
intents.message_content = True  # privileged — enable it in the developer portal

bot = commands.Bot(
    command_prefix=COMMAND_PREFIX,
    intents=intents,
    case_insensitive=True,
    help_command=None,  # replaced by our own .help / .h below
    description="meatbot",
)


class ChannelNotAllowed(commands.CheckFailure):
    """Raised when a command is used outside ALLOWED_CHANNELS."""


def channel_allowed(channel: discord.abc.GuildChannel) -> bool:
    if not ALLOWED_CHANNELS:
        return True
    return channel.id in ALLOWED_CHANNELS or channel.name in ALLOWED_CHANNELS


def allowed_channel_list(guild: discord.Guild) -> str:
    """Render ALLOWED_CHANNELS as #mentions where the channel still exists."""
    parts: list[str] = []
    for entry in ALLOWED_CHANNELS:
        if isinstance(entry, int):
            found = guild.get_channel(entry)
        else:
            found = discord.utils.get(guild.text_channels, name=entry)
        parts.append(found.mention if found else f"#{entry}")
    return ", ".join(sorted(parts))


@bot.check
async def restrict_to_allowed_channels(ctx: commands.Context) -> bool:
    # DMs fall through so @commands.guild_only() produces the real error.
    if ctx.guild is None:
        return True
    # Lobbies the bot made always work, wherever they live, as does The
    # Basement. The server owner isn't restricted at all — it's their server,
    # and .fightclub needs to be runnable anywhere.
    if ctx.channel.id in races or ctx.channel.id in fight_clubs:
        return True
    if ctx.author.id == ctx.guild.owner_id or channel_allowed(ctx.channel):
        return True
    raise ChannelNotAllowed(
        f"meatbot only listens in {allowed_channel_list(ctx.guild)}."
    )


@bot.event
async def on_ready():
    print(f"meatbot online as {bot.user} — prefix '{COMMAND_PREFIX}'")


@bot.event
async def on_message(message: discord.Message):
    # A finished lobby is on a deletion timer. Any human activity pushes it
    # back, so "deleted after N minutes of inactivity" is literally true.
    # This runs before process_commands so that .new, which cancels the timer
    # outright, isn't undone by its own invoking message.
    if not message.author.bot:
        race = races.get(message.channel.id)
        if race is not None and race.cleanup_task is not None:
            race.cleanup_task.cancel()
            race.cleanup_task = asyncio.create_task(
                cleanup_channel(message.channel, CLEANUP_MINUTES * 60)
            )
    await bot.process_commands(message)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    """Forget races whose channel was deleted by hand."""
    race = races.pop(channel.id, None)
    if race:
        if race.countdown_task:
            race.countdown_task.cancel()
        race.finished_event.set()  # don't leave the fight club loop waiting
    fight_clubs.pop(channel.id, None)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    # People type '.' messages casually; don't yell about every non-command.
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("meatbot only works in servers.")
        return
    if isinstance(error, ChannelNotAllowed):
        await nope(ctx, str(error))
        return
    if isinstance(error, commands.CheckFailure):
        return

    # Anything else is a real bug or a permissions problem. Say so in the
    # channel rather than only dumping a traceback to the console, otherwise
    # commands look like they silently do nothing.
    original = getattr(error, "original", error)
    traceback.print_exception(type(original), original, original.__traceback__)
    with contextlib.suppress(discord.HTTPException):
        await ctx.send(f"⚠️ `{COMMAND_PREFIX}{ctx.invoked_with}` failed: {type(original).__name__}")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


async def nope(ctx: commands.Context, message: str) -> None:
    """Short-lived error reply so race channels stay readable."""
    await ctx.reply(message, mention_author=False, delete_after=ERROR_LINGER or None)


async def require_race(
    ctx: commands.Context,
    *,
    states: set[RaceState] | None = None,
) -> Race | None:
    race = races.get(ctx.channel.id)
    if race is None:
        await nope(ctx, f"This isn't a race channel. Use `{COMMAND_PREFIX}make` to start one.")
        return None
    if states and race.state not in states:
        await nope(ctx, f"You can't do that right now — the race is **{race.state.value}**.")
        return None
    return race


def mentioned_members(ctx: commands.Context) -> list[discord.Member]:
    """Members @-mentioned in the command message, minus the author and bots.

    Read straight off the message payload, so no Server Members intent is needed.
    """
    seen: dict[int, discord.Member] = {}
    for member in ctx.message.mentions:
        if not isinstance(member, discord.Member):
            continue
        if member.bot or member.id == ctx.author.id:
            continue
        seen[member.id] = member
    return list(seen.values())


async def require_admin(ctx: commands.Context, race: Race) -> bool:
    if ctx.author.id != race.admin_id:
        await nope(ctx, f"Only the race admin (<@{race.admin_id}>) can do that.")
        return False
    return True


async def refresh_status(race: Race, channel: discord.TextChannel) -> None:
    if race.status_message_id is None:
        return
    message = channel.get_partial_message(race.status_message_id)
    with contextlib.suppress(discord.HTTPException):
        await message.edit(embed=build_status_embed(race))


def maybe_start_countdown(race: Race, channel: discord.TextChannel) -> bool:
    """Public lobbies start themselves once everyone's ready.

    Call while holding race.lock. Lobbies with auto_start off (private ones,
    normally) wait for the admin's .start. Returns True if a countdown began.
    """
    if not race.auto_start or race.state is not RaceState.OPEN:
        return False
    if len(race.entrants) < MIN_ENTRANTS or race.not_ready:
        return False
    race.state = RaceState.COUNTDOWN
    race.countdown_task = asyncio.create_task(run_countdown(race, channel))
    return True


def abort_countdown(race: Race) -> bool:
    """Call while holding race.lock. Returns True if a countdown was actually aborted."""
    if race.state is not RaceState.COUNTDOWN:
        return False
    race.state = RaceState.OPEN
    if race.countdown_task:
        race.countdown_task.cancel()
        race.countdown_task = None
    return True


async def run_countdown(race: Race, channel: discord.TextChannel) -> None:
    deadline = time.monotonic() + COUNTDOWN_SECONDS
    # Same instant on the wall clock, for Discord's relative timestamp.
    start_epoch = round(time.time() + COUNTDOWN_SECONDS)
    # Everything posted while counting down, cleaned up if we get stopped.
    posted: list[discord.Message] = []
    try:
        # <t:..:R> ticks down live in every client with no further API calls,
        # so the early seconds cost one message instead of one per second.
        with contextlib.suppress(discord.HTTPException):
            posted.append(
                await channel.send(f"@here the race will start <t:{start_epoch}:R>")
            )

        # Posting takes a moment, so aim each message early by however long the
        # previous one took to send. Without this every number lands late by
        # one round trip, which is what reads as lag.
        lead = 0.0
        for n in range(min(COUNTDOWN_TICK_FROM, COUNTDOWN_SECONDS), 0, -1):
            await asyncio.sleep(max(0.0, deadline - n - lead - time.monotonic()))
            sent_at = time.monotonic()
            with contextlib.suppress(discord.HTTPException):
                posted.append(await channel.send(f"# {n}"))
            lead = min(time.monotonic() - sent_at, 0.75)

        await asyncio.sleep(max(0.0, deadline - lead - time.monotonic()))

        # No awaits between here and the state change, so nothing can interleave.
        race.state = RaceState.IN_PROGRESS
        race.start_deadline = deadline
        race.started_at = datetime.now(timezone.utc)
        race.countdown_task = None

        with contextlib.suppress(discord.HTTPException):
            await channel.send("# GO! 🏁")
        await refresh_status(race, channel)

    except asyncio.CancelledError:
        # Say it's off first. Deleting costs an API round trip and the notice
        # shouldn't be stuck behind it.
        if race.private:
            note = (
                f"⛔ Countdown stopped. <@{race.admin_id}> will need to "
                f"`{COMMAND_PREFIX}start` again once everyone is ready."
            )
        else:
            note = (
                f"⛔ Countdown stopped. It'll go again on the next ready-up, "
                f"or <@{race.admin_id}> can `{COMMAND_PREFIX}start`."
            )
        with contextlib.suppress(discord.HTTPException):
            await channel.send(note)

        # Clear the stale countdown. Match on content rather than trusting
        # `posted` alone: if a send was still in flight when the cancel landed,
        # the message reached Discord but never made it into the list — which
        # is why the final number tended to survive.
        expected = {f"@here the race will start <t:{start_epoch}:R>"}
        expected.update(f"# {n}" for n in range(1, COUNTDOWN_TICK_FROM + 1))
        tracked = {msg.id for msg in posted}
        me_id = channel.guild.me.id

        def is_countdown_message(msg: discord.Message) -> bool:
            if msg.author.id != me_id:
                return False
            return msg.id in tracked or msg.content in expected

        # Anchor just before our first message so an earlier countdown in this
        # channel is left alone. `after` is exclusive, hence the -1.
        after = discord.Object(id=posted[0].id - 1) if posted else None
        with contextlib.suppress(discord.HTTPException):
            await channel.purge(
                limit=COUNTDOWN_TICK_FROM + 10,
                check=is_countdown_message,
                after=after,
                reason="Countdown stopped",
            )
        raise


def reset_race(race: Race) -> None:
    """Wipe the race back to a fresh, empty lobby. Call while holding race.lock.

    The seed and the admin survive — reroll with .setseed if you want a new one.
    Any pending channel deletion is called off, since the lobby lives on.
    """
    race.entrants.clear()
    race.state = RaceState.OPEN
    race.finished_event.clear()
    race.start_deadline = None
    race.started_at = None
    race.countdown_task = None
    if race.cleanup_task:
        race.cleanup_task.cancel()
        race.cleanup_task = None


async def finish_race(
    race: Race, channel: discord.TextChannel, *, show_results: bool = True
) -> None:
    """End the race and leave the lobby sitting in FINISHED.

    Call while holding race.lock. Entrants are left alone so the status board
    keeps showing the final standings; .new is what clears them for the next
    race. show_results=False is for a race that never actually started.
    """
    race.state = RaceState.FINISHED
    race.finished_event.set()
    if show_results:
        await channel.send(embed=build_results_embed(race))
    await refresh_status(race, channel)
    if CLEANUP_MINUTES > 0:
        await channel.send(
            f"To start a new race, the admin can type `{COMMAND_PREFIX}new`. "
            f"This channel will be deleted after {CLEANUP_MINUTES} minutes of inactivity."
        )
        race.cleanup_task = asyncio.create_task(
            cleanup_channel(channel, CLEANUP_MINUTES * 60)
        )


async def cleanup_channel(channel: discord.TextChannel, delay: float) -> None:
    await asyncio.sleep(delay)
    races.pop(channel.id, None)
    with contextlib.suppress(discord.HTTPException):
        await channel.delete(reason="Race finished")


# --------------------------------------------------------------------------- #
# Opening races
# --------------------------------------------------------------------------- #


async def send_intro_embed(
    race: Race, channel: discord.TextChannel, admin_display: str
) -> None:
    intro = build_lobby_commands_embed(
        race.private,
        f"{'Private ' if race.private else 'Public '}Race Lobby",
    )
    intro.set_footer(text=f"Admin: {admin_display}")
    await channel.send(embed=intro)


async def send_status_board(
    race: Race, channel: discord.TextChannel
) -> discord.Message:
    """Post a fresh live status message and make it the one we keep updating."""
    message = await channel.send(embed=build_status_embed(race))
    race.status_message_id = message.id
    return message


async def pin_status_board(
    message: discord.Message, channel: discord.TextChannel
) -> None:
    # Since Jan 2026 pinning needs the separate "Pin Messages" permission —
    # Manage Messages is no longer enough — so say something rather than
    # failing silently.
    try:
        await message.pin(reason="Live race status")
    except discord.Forbidden:
        await channel.send(
            "⚠️ I couldn't pin the status board — my role needs the "
            "**Pin Messages** permission (it's separate from Manage Messages now)."
        )
    except discord.HTTPException as exc:
        print(f"[meatbot] couldn't pin status message in #{channel.name}: {exc}")


def lobby_overwrites(
    guild: discord.Guild,
    admin: discord.Member,
    *,
    private: bool,
    invitees: list[discord.Member] | None = None,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """Channel overwrites for a race lobby.

    The bot and the admin always get explicit access. Without this, a lobby
    created from a channel in a restricted category inherits that category's
    denies and the bot can't even post in the channel it just made (403
    Missing Access).
    """
    bot_overwrite = discord.PermissionOverwrite(
        view_channel=True, send_messages=True,
        manage_messages=True, manage_channels=True,
    )
    # pin_messages only exists in discord.py >= 2.7; ignore it on older ones.
    if "pin_messages" in discord.PermissionOverwrite.VALID_NAMES:
        bot_overwrite.pin_messages = True

    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.me: bot_overwrite,
        admin: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    if private:
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        for member in invitees or []:
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )
    return overwrites


async def open_race(
    ctx: commands.Context,
    name: str | None,
    *,
    private: bool,
    invitees: list[discord.Member] | None = None,
) -> None:
    guild = ctx.guild
    assert guild is not None
    invitees = invitees or []

    race_id = slugify(name) if name else ""
    if not race_id:
        race_id = random_race_id()

    # Don't collide with an existing race channel of the same name.
    if discord.utils.get(guild.text_channels, name=f"{CHANNEL_PREFIX}-{race_id}"):
        race_id = f"{race_id}-{random_race_id()[:2]}"

    # Sit the lobby alongside whatever channel the command came from. None here
    # is fine — it just means the source channel isn't in a category either.
    category = ctx.channel.category

    overwrites = lobby_overwrites(
        guild, ctx.author, private=private, invitees=invitees
    )

    try:
        channel = await guild.create_text_channel(
            name=f"{CHANNEL_PREFIX}-{race_id}",
            category=category,
            overwrites=overwrites,
            topic=f"{'Private r' if private else 'R'}ace opened by {ctx.author.display_name}",
            reason=f"Race opened by {ctx.author}",
        )
    except discord.Forbidden:
        await nope(ctx, "I don't have permission to create a channel there.")
        return
    except discord.HTTPException as exc:
        await nope(ctx, f"Discord rejected the channel: {exc.text or exc}")
        return

    race = Race(
        race_id=race_id,
        guild_id=guild.id,
        channel_id=channel.id,
        admin_id=ctx.author.id,
        seed=generate_seed(),
        private=private,
        auto_start=not private,
    )
    races[channel.id] = race

    try:
        await send_intro_embed(race, channel, ctx.author.display_name)

        if private:
            note = (
                f"<@{ctx.author.id}> this lobby is invite-only. "
                f"`{COMMAND_PREFIX}adduser @user` to let more people in, "
                f"`{COMMAND_PREFIX}removeuser @user` to kick them out."
            )
            if invitees:
                mentions = " ".join(m.mention for m in invitees)
                note = f"{mentions} — you've been invited to this race.\n{note}"
            await channel.send(note)

        status_message = await send_status_board(race, channel)
    except discord.Forbidden:
        # Don't strand an empty channel nobody can use.
        races.pop(channel.id, None)
        with contextlib.suppress(discord.HTTPException):
            await channel.delete(reason="Lobby setup failed: no access")
        await nope(
            ctx,
            "I made the lobby but couldn't post in it, so I've removed it. "
            "My role needs **View Channel** and **Send Messages** in this category.",
        )
        return

    await pin_status_board(status_message, channel)

    await ctx.reply(
        f"{'Private race' if private else 'Race'} lobby opened: {channel.mention}",
        mention_author=False,
    )


@bot.command(name="make")
@commands.guild_only()
async def make(ctx: commands.Context, *, name: str | None = None):
    """Open a public race lobby."""
    await open_race(ctx, name, private=False)


@bot.command(name="makeprivate")
@commands.guild_only()
async def makeprivate(ctx: commands.Context, *, name: str | None = None):
    """Open an invite-only lobby. Any @mentions get access straight away."""
    await open_race(ctx, name, private=True, invitees=mentioned_members(ctx))


@bot.command(name="adduser")
@commands.guild_only()
async def adduser(ctx: commands.Context, *, _mentions: str | None = None):
    """Admin: give @someone access to this lobby."""
    race = await require_race(ctx, states={RaceState.OPEN, RaceState.COUNTDOWN})
    if race is None:
        return
    if not await require_admin(ctx, race):
        return
    if not race.private:
        await nope(ctx, "This lobby is public — anyone can already see it.")
        return

    members = mentioned_members(ctx)
    if not members:
        await nope(ctx, f"Mention someone to add, e.g. `{COMMAND_PREFIX}adduser @racer`.")
        return

    for member in members:
        try:
            await ctx.channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                reason=f"Added to lobby by {ctx.author}",
            )
        except discord.Forbidden:
            await nope(ctx, "I need **Manage Roles** here to change channel permissions.")
            return

    mentions = ", ".join(m.mention for m in members)
    await ctx.send(
        f"✅ {mentions} added to the lobby. `{COMMAND_PREFIX}enter` (`.e`) to join in."
    )


@bot.command(name="removeuser")
@commands.guild_only()
async def removeuser(ctx: commands.Context, *, _mentions: str | None = None):
    """Admin: kick @someone out of this lobby. Private lobbies only."""
    race = await require_race(ctx, states={RaceState.OPEN, RaceState.COUNTDOWN})
    if race is None:
        return
    if not await require_admin(ctx, race):
        return
    if not race.private:
        await nope(ctx, "This lobby is public — anyone can join, so there's nobody to remove.")
        return

    members = mentioned_members(ctx)
    if not members:
        await nope(ctx, f"Mention someone to remove, e.g. `{COMMAND_PREFIX}removeuser @racer`.")
        return

    async with race.lock:
        for member in members:
            # Drop their overwrite entirely — @everyone can't see the channel,
            # so losing the allow is enough to shut them out.
            try:
                await ctx.channel.set_permissions(
                    member, overwrite=None, reason=f"Removed from lobby by {ctx.author}"
                )
            except discord.Forbidden:
                await nope(ctx, "I need **Manage Roles** here to change channel permissions.")
                return
            race.entrants.pop(member.id, None)

        abort_countdown(race)
        mentions = ", ".join(m.mention for m in members)
        await ctx.send(f"👋 {mentions} removed from the lobby.")
        await refresh_status(race, ctx.channel)


@bot.command(name="setadmin")
@commands.guild_only()
async def setadmin(ctx: commands.Context, *, _mention: str | None = None):
    """Admin: hand the lobby over to someone else."""
    race = await require_race(ctx)
    if race is None:
        return
    if not await require_admin(ctx, race):
        return

    # mentioned_members drops the author and bots, so .setadmin @yourself just
    # comes back empty rather than being a confusing no-op.
    members = mentioned_members(ctx)
    if not members:
        await nope(
            ctx, f"Mention who should take over, e.g. `{COMMAND_PREFIX}setadmin @racer`."
        )
        return
    if len(members) > 1:
        await nope(ctx, "One at a time — a lobby has a single admin.")
        return

    member = members[0]
    if not ctx.channel.permissions_for(member).view_channel:
        await nope(
            ctx,
            f"**{member.display_name}** can't see this lobby — "
            f"`{COMMAND_PREFIX}adduser` them first.",
        )
        return

    async with race.lock:
        race.admin_id = member.id
        await ctx.send(f"👑 {member.mention} is now the admin of this lobby.")
        await refresh_status(race, ctx.channel)


@bot.command(name="setseed")
@commands.guild_only()
async def setseed(ctx: commands.Context, *, seed: str | None = None):
    """Admin: replace the seed. Blank rerolls a random one."""
    race = await require_race(ctx, states={RaceState.OPEN, RaceState.COUNTDOWN})
    if race is None:
        return
    if not await require_admin(ctx, race):
        return

    new_seed: int | None = None
    if seed is not None:
        # Tolerate the separators people paste: "4,294,967,295", "4_294_967_295".
        cleaned = seed.strip().replace(",", "").replace("_", "").replace(" ", "")
        # isdecimal() rather than isdigit(): the latter accepts things like "²"
        # that int() then chokes on.
        if not cleaned.isdecimal():
            await nope(
                ctx,
                f"Seeds are whole numbers from {SEED_MIN} to {SEED_MAX:,} — "
                f"`{COMMAND_PREFIX}setseed` on its own rolls a random one.",
            )
            return
        new_seed = int(cleaned)
        if not SEED_MIN <= new_seed <= SEED_MAX:
            await nope(ctx, f"Seed is out of range — it has to be {SEED_MIN} to {SEED_MAX:,}.")
            return

    async with race.lock:
        race.seed = new_seed if new_seed is not None else generate_seed()

        unreadied = 0
        for entrant in race.entrants.values():
            if entrant.state is EntrantState.READY:
                entrant.state = EntrantState.ENTERED
                unreadied += 1
        abort_countdown(race)  # if a countdown was running it announces its own abort

        message = f"🎲 New seed: `{race.seed}`"
        if unreadied:
            message += (
                f" — {unreadied} runner{'s' if unreadied != 1 else ''} unreadied. "
                f"`{COMMAND_PREFIX}ready` (`.r`) again once you've loaded it."
            )
        await ctx.send(message)
        await refresh_status(race, ctx.channel)


# --------------------------------------------------------------------------- #
# Entering and readying
# --------------------------------------------------------------------------- #


@bot.command(name="enter", aliases=["e"])
@commands.guild_only()
async def enter(ctx: commands.Context):
    """Join the race in this channel, or the fight club if this is The Basement."""
    club = fight_clubs.get(ctx.channel.id)
    if club is not None:
        await fight_club_enter(ctx, club)
        return
    race = await require_race(ctx, states={RaceState.OPEN, RaceState.COUNTDOWN})
    if race is None:
        return
    async with race.lock:
        if ctx.author.id in race.entrants:
            await nope(ctx, "You're already entered.")
            return
        race.entrants[ctx.author.id] = Entrant(user_id=ctx.author.id)
        # A fresh entrant isn't ready, so any running countdown has to stop.
        abort_countdown(race)  # the countdown task announces its own abort
        await ctx.send(
            f"**{ctx.author.display_name}** has entered "
            f"({len(race.entrants)} entrant{'s' if len(race.entrants) != 1 else ''}). "
            f"`{COMMAND_PREFIX}ready` when you're set up."
        )
        await refresh_status(race, ctx.channel)


async def do_unenter(ctx: commands.Context, race: Race) -> None:
    async with race.lock:
        if ctx.author.id not in race.entrants:
            await nope(ctx, "You aren't entered.")
            return
        del race.entrants[ctx.author.id]
        abort_countdown(race)  # if a countdown was running it announces its own abort
        await ctx.send(f"**{ctx.author.display_name}** has left the race.")
        # Whoever's left might now all be ready.
        maybe_start_countdown(race, ctx.channel)
        await refresh_status(race, ctx.channel)


async def do_unready(ctx: commands.Context, race: Race) -> None:
    async with race.lock:
        entrant = race.entrants.get(ctx.author.id)
        if entrant is None or entrant.state is not EntrantState.READY:
            await nope(ctx, "You aren't marked ready.")
            return
        entrant.state = EntrantState.ENTERED
        abort_countdown(race)  # if a countdown was running it announces its own abort
        await ctx.send(f"**{ctx.author.display_name}** is no longer ready.")
        await refresh_status(race, ctx.channel)


@bot.command(name="ready", aliases=["r"])
@commands.guild_only()
async def ready(ctx: commands.Context):
    """Mark yourself ready to start."""
    race = await require_race(ctx, states={RaceState.OPEN})
    if race is None:
        return
    async with race.lock:
        entrant = race.entrants.get(ctx.author.id)
        if entrant is None:
            await nope(ctx, f"You need to `{COMMAND_PREFIX}enter` first.")
            return
        if entrant.state is EntrantState.READY:
            await nope(ctx, "You're already ready.")
            return

        entrant.state = EntrantState.READY
        outstanding = len(race.not_ready)
        started = maybe_start_countdown(race, ctx.channel)
        if outstanding:
            tail = f" Waiting on {outstanding} more."
        elif len(race.entrants) < MIN_ENTRANTS:
            tail = f" Need at least {MIN_ENTRANTS} entrants before this can start."
        elif started:
            tail = ""  # the countdown announcement lands right after this
        else:
            tail = f" Everyone's ready — <@{race.admin_id}> can `{COMMAND_PREFIX}start`."
        await ctx.send(f"**{ctx.author.display_name}** is ready.{tail}")
        await refresh_status(race, ctx.channel)


@bot.command(name="unready")
@commands.guild_only()
async def unready(ctx: commands.Context):
    """Take back your ready status."""
    race = await require_race(ctx, states={RaceState.OPEN, RaceState.COUNTDOWN})
    if race is None:
        return
    await do_unready(ctx, race)


@bot.command(name="unenter", aliases=["quit"])
@commands.guild_only()
async def unenter(ctx: commands.Context):
    """Leave the race before it starts."""
    club = fight_clubs.get(ctx.channel.id)
    if club is not None:
        await fight_club_leave(ctx, club)
        return
    race = await require_race(ctx, states={RaceState.OPEN, RaceState.COUNTDOWN})
    if race is None:
        return
    await do_unenter(ctx, race)


@bot.command(name="u")
@commands.guild_only()
async def undo_step(ctx: commands.Context):
    """Step back one level: unready if you're ready, otherwise unenter."""
    club = fight_clubs.get(ctx.channel.id)
    if club is not None:
        await fight_club_leave(ctx, club)
        return
    race = await require_race(ctx, states={RaceState.OPEN, RaceState.COUNTDOWN})
    if race is None:
        return
    entrant = race.entrants.get(ctx.author.id)
    if entrant is None:
        await nope(ctx, "You aren't entered.")
        return
    if entrant.state is EntrantState.READY:
        await do_unready(ctx, race)
    else:
        await do_unenter(ctx, race)


# --------------------------------------------------------------------------- #
# Running the race
# --------------------------------------------------------------------------- #


@bot.command(name="start")
@commands.guild_only()
async def start(ctx: commands.Context):
    """Admin: begin the countdown."""
    race = await require_race(ctx, states={RaceState.OPEN})
    if race is None:
        return
    if not await require_admin(ctx, race):
        return

    async with race.lock:
        count = len(race.entrants)
        if count < MIN_ENTRANTS:
            await nope(
                ctx,
                f"Need at least {MIN_ENTRANTS} entrant{'s' if MIN_ENTRANTS != 1 else ''} "
                f"to start — there {'is' if count == 1 else 'are'} {count}.",
            )
            return

        stragglers = race.not_ready
        if stragglers:
            mentions = " ".join(e.mention for e in stragglers)
            await ctx.send(
                f"{mentions} — the admin wants to start. "
                f"`{COMMAND_PREFIX}ready` (`.r`) when you're set up."
            )
            return

        race.state = RaceState.COUNTDOWN
        race.countdown_task = asyncio.create_task(run_countdown(race, ctx.channel))


@bot.command(name="stop")
@commands.guild_only()
async def stop(ctx: commands.Context):
    """Admin: stop a countdown that's already running."""
    race = await require_race(ctx, states={RaceState.COUNTDOWN})
    if race is None:
        return
    if not await require_admin(ctx, race):
        return

    async with race.lock:
        # Deliberately no maybe_start_countdown() here. In a public lobby
        # everyone is still ready, so re-checking would relaunch the countdown
        # instantly. It stays stopped until someone does something: an admin
        # .start, or the next enter/unenter/ready/unready.
        abort_countdown(race)  # the countdown task announces its own stop
        await refresh_status(race, ctx.channel)


@bot.command(name="done", aliases=["d"])
@commands.guild_only()
async def done(ctx: commands.Context):
    """Stop your timer — you've finished."""
    existing = races.get(ctx.channel.id)
    elapsed = existing.elapsed() if existing else 0.0  # read the clock before any awaits

    race = await require_race(ctx, states={RaceState.IN_PROGRESS})
    if race is None:
        return
    async with race.lock:
        entrant = race.entrants.get(ctx.author.id)
        if entrant is None:
            await nope(ctx, "You aren't in this race.")
            return
        if entrant.state is EntrantState.DONE:
            await nope(ctx, f"You already finished in {format_time(entrant.finish_time or 0)}.")
            return

        entrant.state = EntrantState.DONE
        entrant.finish_time = elapsed
        place = len(race.finishers)
        icon = PLACE_ICONS.get(place, "🏁")
        await ctx.send(
            f"{icon} {place}{PLACE_SUFFIXES.get(place, 'th')} - **{ctx.author.display_name}** finishes in "
            f"**{format_time(elapsed)}**"
        )
        await refresh_status(race, ctx.channel)

        if race.everyone_finished:
            await finish_race(race, ctx.channel)


@bot.command(name="forfeit", aliases=["f", "ff"])
@commands.guild_only()
async def forfeit(ctx: commands.Context):
    """Drop out of the race."""
    race = await require_race(ctx, states={RaceState.IN_PROGRESS, RaceState.COUNTDOWN})
    if race is None:
        return
    async with race.lock:
        entrant = race.entrants.get(ctx.author.id)
        if entrant is None:
            await nope(ctx, "You aren't in this race.")
            return
        if entrant.state is EntrantState.FORFEIT:
            await nope(ctx, "You've already forfeited.")
            return

        entrant.state = EntrantState.FORFEIT
        entrant.finish_time = None
        abort_countdown(race)
        await ctx.send(f"🏳️ **{ctx.author.display_name}** has forfeited.")
        await refresh_status(race, ctx.channel)

        if race.state is RaceState.IN_PROGRESS and race.everyone_finished:
            await finish_race(race, ctx.channel)


@bot.command(name="undone", aliases=["z", "unforfeit"])
@commands.guild_only()
async def undone(ctx: commands.Context):
    """Undo a done/forfeit and keep racing."""
    race = await require_race(ctx, states={RaceState.IN_PROGRESS})
    if race is None:
        return
    async with race.lock:
        entrant = race.entrants.get(ctx.author.id)
        if entrant is None or entrant.state not in (EntrantState.DONE, EntrantState.FORFEIT):
            await nope(ctx, "Nothing to undo.")
            return
        entrant.state = EntrantState.ENTERED
        entrant.finish_time = None
        await ctx.send(f"**{ctx.author.display_name}** is back in the race.")
        await refresh_status(race, ctx.channel)


# --------------------------------------------------------------------------- #
# Info / admin
# --------------------------------------------------------------------------- #


@bot.command(name="info", aliases=["i"])
@commands.guild_only()
async def info(ctx: commands.Context):
    """Show the current state of this race, or the fight club leaderboard."""
    club = fight_clubs.get(ctx.channel.id)
    if club is not None:
        await ctx.send(embed=build_leaderboard_embed(club))
        return
    race = await require_race(ctx)
    if race is None:
        return
    embed = build_status_embed(race)
    if race.state is RaceState.IN_PROGRESS:
        embed.add_field(name="Elapsed", value=format_time(race.elapsed()), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="new")
@commands.guild_only()
async def new_race(ctx: commands.Context):
    """Admin: run another race in this lobby, with a fresh seed."""
    race = await require_race(ctx, states={RaceState.FINISHED})
    if race is None:
        return
    if not await require_admin(ctx, race):
        return

    async with race.lock:
        reset_race(race)  # also calls off any pending channel deletion
        race.race_number += 1
        race.seed = generate_seed()

        await ctx.send(f"🆕 **Race #{race.race_number}** starting in this lobby.")
        await send_intro_embed(race, ctx.channel, ctx.author.display_name)
        # A brand new board, pinned. The old ones stay put as a record of the
        # races this lobby has run.
        status_message = await send_status_board(race, ctx.channel)
        await pin_status_board(status_message, ctx.channel)


@bot.command(name="forcefinish")
@commands.guild_only()
async def forcefinish(ctx: commands.Context):
    """Admin: end the race whatever state it's in and reset the lobby."""
    race = await require_race(ctx)
    if race is None:
        return
    if not await require_admin(ctx, race):
        return
    # Without this the command happily "ends" an already-ended race: the state
    # check passes and start_deadline is still set, so it reposts the same
    # results every time it's run.
    if race.state is RaceState.FINISHED:
        await nope(
            ctx,
            f"Race #{race.race_number} has already finished — "
            f"`{COMMAND_PREFIX}new` starts the next one.",
        )
        return

    async with race.lock:
        abort_countdown(race)
        # start_deadline is the honest test of "did this race actually begin",
        # and it survives into FINISHED, unlike the state itself.
        had_begun = race.start_deadline is not None

        if had_begun:
            for entrant in race.entrants.values():
                if entrant.state in (EntrantState.ENTERED, EntrantState.READY):
                    entrant.state = EntrantState.UNFINISHED
            await ctx.send("🛑 Race ended by the admin.")
        else:
            await ctx.send("🛑 Race called off before it started.")

        # Lands in FINISHED exactly like a race that ended on its own, so the
        # lobby waits for .new rather than silently reopening. The race number
        # is bumped by .new, not here, or the next race would skip a number.
        await finish_race(race, ctx.channel, show_results=had_begun)


@bot.command(name="help", aliases=["h"])
async def help_command(ctx: commands.Context):
    """Show the commands that apply where you typed this."""
    race = races.get(ctx.channel.id)
    if race is not None:
        embed = build_lobby_commands_embed(race.private, f"{'Private ' if race.private else 'Public '}Race Lobby")
    else:
        embed = discord.Embed(
            title="meatbot",
            description="\n".join(format_command(*row) for row in GENERAL_COMMANDS),
            colour=discord.Colour.blurple(),
        )
    await ctx.send(embed=embed)


# --------------------------------------------------------------------------- #
# The Basement
#
# Unadvertised, server-owner only. Turns a channel into a sign-up lobby that
# repeatedly pairs two fighters into a private head-to-head race, one race at a
# time, until the owner calls it off.
# --------------------------------------------------------------------------- #


@dataclass
class Fighter:
    user_id: int
    races: int = 0
    wins: int = 0
    active: bool = True  # False once they .u out; stats are kept either way

    @property
    def mention(self) -> str:
        return f"<@{self.user_id}>"


@dataclass
class FightClub:
    guild_id: int
    channel_id: int          # The Basement
    owner_id: int
    fighters: dict[int, Fighter] = field(default_factory=dict)
    status_message_id: int | None = None
    match_number: int = 0
    running: bool = True
    current_race: Race | None = None
    loop_task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def active_fighters(self) -> list[Fighter]:
        return [f for f in self.fighters.values() if f.active]


# basement channel_id -> FightClub
fight_clubs: dict[int, FightClub] = {}


def is_server_owner(ctx: commands.Context) -> bool:
    return ctx.guild is not None and ctx.author.id == ctx.guild.owner_id


def build_leaderboard_embed(club: FightClub, *, final: bool = False) -> discord.Embed:
    embed = discord.Embed(
        title="🥊 The Basement — final standings" if final else "🥊 The Basement",
        colour=discord.Colour.dark_red() if final else discord.Colour.red(),
    )
    # Most wins first, then fewest races (a better ratio), then join order.
    ranked = sorted(
        club.fighters.values(),
        key=lambda f: (-f.wins, f.races, f.user_id),
    )
    if ranked:
        lines = []
        for i, f in enumerate(ranked, start=1):
            icon = PLACE_ICONS.get(i, f"`{i}.`")
            tail = "" if f.active else " *(left)*"
            lines.append(
                f"{icon} {f.mention} — **{f.wins}** win{'' if f.wins == 1 else 's'} "
                f"/ {f.races} race{'' if f.races == 1 else 's'}{tail}"
            )
        embed.description = "\n".join(lines)
    else:
        embed.description = f"*Nobody yet — `{COMMAND_PREFIX}enter` to join.*"

    if not final:
        embed.add_field(name="Fighters in", value=str(len(club.active_fighters)))
        embed.add_field(name="Matches run", value=str(club.match_number))
    return embed


async def refresh_leaderboard(club: FightClub, channel: discord.TextChannel) -> None:
    if club.status_message_id is None:
        return
    message = channel.get_partial_message(club.status_message_id)
    with contextlib.suppress(discord.HTTPException):
        await message.edit(embed=build_leaderboard_embed(club))


def pick_pair(club: FightClub) -> list[Fighter] | None:
    """Two fighters, drawn from whoever has raced least.

    Shuffle first, then sort by race count: the sort is stable, so within the
    least-played group the order stays random, and if that group has an odd
    number the extra slot falls through to the next group up.
    """
    pool = club.active_fighters
    if len(pool) < 2:
        return None
    random.shuffle(pool)
    pool.sort(key=lambda f: f.races)
    return pool[:2]


async def run_fight(
    club: FightClub, channel: discord.TextChannel, pair: list[Fighter]
) -> None:
    """Build a private room for the pair and block until their race is over."""
    guild = channel.guild
    club.match_number += 1
    race_id = f"fight-{club.match_number}"

    owner = guild.get_member(club.owner_id)
    members = [guild.get_member(f.user_id) for f in pair]
    if owner is None or any(m is None for m in members):
        await channel.send("⚠️ Couldn't find one of the fighters in the server — skipping.")
        return

    overwrites = lobby_overwrites(guild, owner, private=True, invitees=members)
    try:
        room = await guild.create_text_channel(
            name=f"{CHANNEL_PREFIX}-{race_id}",
            category=channel.category,
            overwrites=overwrites,
            topic=f"Fight club match {club.match_number}",
            reason="Fight club match",
        )
    except discord.HTTPException as exc:
        await channel.send(f"⚠️ Couldn't create the room: {exc.text or exc}")
        return

    race = Race(
        race_id=race_id,
        guild_id=guild.id,
        channel_id=room.id,
        admin_id=club.owner_id,
        seed=generate_seed(),
        race_number=club.match_number,
        private=True,
        auto_start=True,  # no babysitting: it goes when both fighters are ready
    )
    races[room.id] = race
    club.current_race = race

    await channel.send(
        f"🥊 **Match {club.match_number}** — {pair[0].mention} vs {pair[1].mention} "
        f"→ {room.mention}"
    )

    with contextlib.suppress(discord.HTTPException):
        await room.send(
            f"{members[0].mention} {members[1].mention} — head to head. "
            f"`{COMMAND_PREFIX}enter` then `{COMMAND_PREFIX}ready`; "
            f"the countdown starts once you both are."
        )
        await send_intro_embed(race, room, owner.display_name)
        status_message = await send_status_board(race, room)
        await pin_status_board(status_message, room)

    # The race ends by everyone finishing, by .forcefinish, or by the channel
    # being deleted — all of which set this.
    await race.finished_event.wait()

    winner_id = race.finishers[0].user_id if race.finishers else None
    async with club.lock:
        for fighter in pair:
            fighter.races += 1
        if winner_id is not None and winner_id in club.fighters:
            club.fighters[winner_id].wins += 1
        club.current_race = None

    if winner_id is not None:
        await channel.send(f"🏆 Match {club.match_number}: <@{winner_id}> takes it.")
    else:
        await channel.send(f"Match {club.match_number} ended with no winner.")
    await refresh_leaderboard(club, channel)


async def fight_club_loop(club: FightClub, channel: discord.TextChannel) -> None:
    try:
        while club.running:
            await asyncio.sleep(FIGHT_CLUB_INTERMISSION)
            if not club.running:
                return
            async with club.lock:
                pair = pick_pair(club)
            if pair is None:
                await channel.send(
                    f"Need 2 fighters to make a match — "
                    f"`{COMMAND_PREFIX}enter` and I'll check again in "
                    f"{FIGHT_CLUB_INTERMISSION} seconds."
                )
                continue
            await run_fight(club, channel, pair)
            if club.running:
                await channel.send(
                    f"Next match in {FIGHT_CLUB_INTERMISSION} seconds. "
                    f"`{COMMAND_PREFIX}enter` while there's still time."
                )
    except asyncio.CancelledError:
        raise


async def fight_club_enter(ctx: commands.Context, club: FightClub) -> None:
    async with club.lock:
        fighter = club.fighters.get(ctx.author.id)
        if fighter is not None and fighter.active:
            await nope(ctx, "You're already in.")
            return
        if fighter is None:
            club.fighters[ctx.author.id] = Fighter(user_id=ctx.author.id)
            await ctx.send(f"🥊 **{ctx.author.display_name}** is in the basement.")
        else:
            fighter.active = True  # rejoining keeps their record
            await ctx.send(f"🥊 **{ctx.author.display_name}** is back in.")
    await refresh_leaderboard(club, ctx.channel)


async def fight_club_leave(ctx: commands.Context, club: FightClub) -> None:
    async with club.lock:
        fighter = club.fighters.get(ctx.author.id)
        if fighter is None or not fighter.active:
            await nope(ctx, "You aren't in.")
            return
        fighter.active = False
        await ctx.send(f"**{ctx.author.display_name}** has left the basement.")
    await refresh_leaderboard(club, ctx.channel)


@bot.command(name="fightclub", hidden=True)
@commands.guild_only()
async def fightclub(ctx: commands.Context):
    # Unadvertised: anyone who isn't the server owner gets nothing at all,
    # exactly as if the command didn't exist.
    if not is_server_owner(ctx):
        return
    if ctx.channel.id in fight_clubs:
        await nope(ctx, "This channel is already the basement.")
        return
    if ctx.channel.id in races:
        await nope(ctx, "Not in a race lobby — pick a normal channel.")
        return

    club = FightClub(
        guild_id=ctx.guild.id,
        channel_id=ctx.channel.id,
        owner_id=ctx.author.id,
    )
    fight_clubs[ctx.channel.id] = club

    intro = discord.Embed(
        title="🥊 Fight club",
        description=(
            f"This channel is now **The Basement**.\n\n"
            f"`{COMMAND_PREFIX}enter` (`.e`) to join, `{COMMAND_PREFIX}u` to drop out. "
            f"You can come and go at any time.\n"
            f"Every {FIGHT_CLUB_INTERMISSION} seconds I'll pull two fighters into a "
            f"private room for a head-to-head race. Whoever has raced least goes first.\n\n"
            f"**First matchup in {FIGHT_CLUB_INTERMISSION} seconds.**"
        ),
        colour=discord.Colour.red(),
    )
    await ctx.send(embed=intro)

    board = await ctx.send(embed=build_leaderboard_embed(club))
    club.status_message_id = board.id
    await pin_status_board(board, ctx.channel)

    club.loop_task = asyncio.create_task(fight_club_loop(club, ctx.channel))


@bot.command(name="stopfightclub", hidden=True)
@commands.guild_only()
async def stopfightclub(ctx: commands.Context):
    if not is_server_owner(ctx):
        return
    club = fight_clubs.get(ctx.channel.id)
    if club is None:
        return

    club.running = False
    race = club.current_race
    if race is not None and race.state is not RaceState.FINISHED:
        # Force-finish the live match so its result still counts.
        room = bot.get_channel(race.channel_id)
        async with race.lock:
            abort_countdown(race)
            for entrant in race.entrants.values():
                if entrant.state in (EntrantState.ENTERED, EntrantState.READY):
                    entrant.state = EntrantState.UNFINISHED
            if room is not None:
                await room.send("🛑 Fight club is over — ending this match.")
                await finish_race(race, room, show_results=race.start_deadline is not None)
            else:
                race.state = RaceState.FINISHED
                race.finished_event.set()
        # Let the loop record the result before we tear it down.
        await asyncio.sleep(0)

    if club.loop_task:
        club.loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await club.loop_task

    fight_clubs.pop(ctx.channel.id, None)
    await ctx.send("🥊 **Fight club is over.**")
    await ctx.send(embed=build_leaderboard_embed(club, final=True))


# --------------------------------------------------------------------------- #

def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    bot.run(token)


if __name__ == "__main__":
    main()