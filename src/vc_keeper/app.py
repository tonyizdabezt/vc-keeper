from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import signal
import sys
from dataclasses import dataclass

import discord

log = logging.getLogger("vc_keeper")

EXIT_OK = 0
EXIT_CRASH = 1
EXIT_CONFIG = 78

HEALTH_CHECK_EVERY_S = 60.0
LIBRARY_RECONNECT_GRACE_S = 30.0
MAX_BACKOFF_S = 60.0
SNOWFLAKE = re.compile(r"^\d{16,22}$")


# --------------------------------------------------------------------------- config


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Config:
    token: str
    guild_id: int
    channel_id: int
    self_deaf: bool
    cooldown_min_s: float
    cooldown_max_s: float
    log_level: str

    @classmethod
    def from_env(cls) -> Config:
        problems: list[str] = []

        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            problems.append("DISCORD_TOKEN is missing")

        def snowflake(name: str) -> int:
            raw = os.environ.get(name, "").strip()
            if not SNOWFLAKE.fullmatch(raw):
                problems.append(f"{name} must be a Discord ID (16-22 digits)")
                return 0
            return int(raw)

        def seconds(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                problems.append(f"{name} must be a number of seconds")
                return default
            if value < 0:
                problems.append(f"{name} can't be negative")
            return value

        guild_id = snowflake("DISCORD_GUILD_ID")
        channel_id = snowflake("DISCORD_CHANNEL_ID")
        cooldown_min = seconds("REJOIN_COOLDOWN_MIN", 10)
        cooldown_max = seconds("REJOIN_COOLDOWN_MAX", 120)
        if cooldown_max < cooldown_min:
            problems.append("REJOIN_COOLDOWN_MAX must be >= REJOIN_COOLDOWN_MIN")

        self_deaf = os.environ.get("SELF_DEAF", "false").strip().lower() in {"1", "true", "yes", "on"}
        log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            problems.append("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")

        if problems:
            raise ConfigError("; ".join(problems))

        return cls(token, guild_id, channel_id, self_deaf, cooldown_min, cooldown_max, log_level)


# --------------------------------------------------------------------------- client


class VcKeeper(discord.Client):
    def __init__(self, config: Config) -> None:
        super().__init__(
            afk=True,
            sync_presence=False,
            max_messages=None,
            guild_subscriptions=False,
            member_cache_flags=discord.MemberCacheFlags(joined=False),
        )
        self.config = config
        self._wake = asyncio.Event()
        self._kicked = False
        self._supervisor: asyncio.Task[None] | None = None
        self._shutdown: asyncio.Task[None] | None = None

    # ---- lifecycle --------------------------------------------------------

    async def setup_hook(self) -> None:
        self._supervisor = asyncio.create_task(self._supervise(), name="vc-keeper-supervisor")
        self._supervisor.add_done_callback(self._on_supervisor_done)

    def _on_supervisor_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self.is_closed():
            return
        log.critical(
            "supervisor died unexpectedly, shutting down so systemd restarts us", exc_info=task.exception()
        )
        self.request_shutdown()

    def request_shutdown(self) -> None:
        if self._shutdown is None:
            self._shutdown = asyncio.get_running_loop().create_task(self.close())

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("logged in as %s (%s)", self.user, self.user.id)
        self._wake.set()

    async def on_resumed(self) -> None:
        log.info("gateway session resumed")
        self._wake.set()

    async def on_voice_state_update(
        self, member: discord.Member | discord.User, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if self.user is None or member.id != self.user.id:
            return
        guild = getattr(after.channel or before.channel, "guild", None)
        if guild is not None and guild.id != self.config.guild_id:
            return

        now = after.channel.id if after.channel else None
        if now == self.config.channel_id:
            log.debug("voice state: in target channel")
            return

        where = f"moved to {now}" if now else "disconnected"
        log.warning("voice state changed: %s", where)
        self._kicked = True
        self._wake.set()

    # ---- the one loop that decides everything -------------------------------

    async def _supervise(self) -> None:
        await self.wait_until_ready()
        failures = 0

        while not self.is_closed():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=HEALTH_CHECK_EVERY_S)
            except TimeoutError:
                pass
            self._wake.clear()

            if self._in_target():
                failures = 0
                self._kicked = False
                continue

            if self._kicked:
                self._kicked = False
                delay = random.uniform(self.config.cooldown_min_s, self.config.cooldown_max_s)
                log.info("rejoining in %.0fs (cooldown)", delay)
                await asyncio.sleep(delay)
                if self._in_target():
                    continue

            try:
                await self._ensure_joined()
                failures = 0
                log.info("in voice channel %s", self.config.channel_id)
            except (discord.ClientException, discord.HTTPException, TimeoutError, OSError) as exc:
                failures += 1
                delay = min(2.0**failures, MAX_BACKOFF_S) * random.uniform(0.8, 1.2)
                log.error(
                    "join attempt %d failed (%s: %s), retrying in %.0fs",
                    failures,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                self._wake.set()

    # ---- helpers --------------------------------------------------------------

    def _voice_client(self) -> discord.VoiceClient | None:
        guild = self.get_guild(self.config.guild_id)
        vc = guild.voice_client if guild else None
        return vc if isinstance(vc, discord.VoiceClient) else None

    def _in_target(self) -> bool:
        vc = self._voice_client()
        return (
            vc is not None and vc.is_connected() and getattr(vc.channel, "id", None) == self.config.channel_id
        )

    def _target_channel(self) -> discord.VoiceChannel | discord.StageChannel:
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            raise discord.ClientException(f"not in guild {self.config.guild_id} (check DISCORD_GUILD_ID)")
        channel = guild.get_channel(self.config.channel_id)
        if not isinstance(channel, discord.VoiceChannel | discord.StageChannel):
            raise discord.ClientException(f"{self.config.channel_id} isn't a voice/stage channel you can see")
        return channel

    async def _ensure_joined(self) -> None:
        channel = self._target_channel()
        vc = self._voice_client()

        if vc is not None:
            if getattr(vc.channel, "id", None) == channel.id:
                log.debug("voice client reconnecting on its own, giving it %.0fs", LIBRARY_RECONNECT_GRACE_S)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + LIBRARY_RECONNECT_GRACE_S
                while loop.time() < deadline:
                    await asyncio.sleep(1)
                    if self._in_target():
                        return
                log.warning("library reconnect didn't finish, starting fresh")
                await vc.disconnect(force=True)
            elif vc.is_connected():
                log.info("moving back to target channel")
                await vc.move_to(channel)
                return
            else:
                await vc.disconnect(force=True)

        await channel.connect(self_mute=True, self_deaf=self.config.self_deaf, reconnect=True, timeout=30.0)


# --------------------------------------------------------------------------- entrypoint


def _setup_logging(level: str) -> None:
    fmt = "%(levelname)-7s %(name)s: %(message)s"
    if "INVOCATION_ID" not in os.environ:
        fmt = "%(asctime)s " + fmt
    logging.basicConfig(level=level, format=fmt, stream=sys.stdout)
    logging.getLogger("discord").setLevel(level if level == "DEBUG" else "WARNING")


async def _run(config: Config) -> None:
    async with VcKeeper(config) as client:
        loop = asyncio.get_running_loop()

        def shutdown(sig: signal.Signals) -> None:
            log.info("got %s, leaving voice and logging out", sig.name)
            client.request_shutdown()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, shutdown, sig)
            except NotImplementedError:
                pass
        await client.start(config.token)


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        logging.basicConfig(format="%(levelname)s %(message)s")
        log.error("config error: %s", exc)
        sys.exit(EXIT_CONFIG)

    _setup_logging(config.log_level)
    log.info("starting vc-keeper (target %s in guild %s)", config.channel_id, config.guild_id)

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        pass
    except discord.LoginFailure:
        log.error("discord rejected the token. not restarting (exit 78). get a fresh token and restart.")
        sys.exit(EXIT_CONFIG)
    except discord.ConnectionClosed as exc:
        if exc.code == 4004:
            log.error("gateway says the token is invalid (4004). not restarting (exit 78).")
            sys.exit(EXIT_CONFIG)
        log.error("gateway closed for good (code %s), exiting so systemd restarts us", exc.code)
        sys.exit(EXIT_CRASH)
    except Exception:
        log.exception("crashed")
        sys.exit(EXIT_CRASH)

    log.info("stopped")
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
