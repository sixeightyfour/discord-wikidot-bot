import os
import time
import aiohttp
import discord
from discord.ext import tasks
from discord import app_commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

CHECK_URL = "https://scp-wiki.wikidot.com/"
CHECK_INTERVAL_SECONDS = 60
FAIL_THRESHOLD = 10


SLOW_RESPONSE_THRESHOLD_SECONDS = 30.0
HTTP_TIMEOUT_SECONDS = 45.0

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

http_session: aiohttp.ClientSession | None = None

consecutive_failures = 0
outage_announced = False
slow_response_announced = False

last_status = "Unknown"
last_detail = "No checks yet"
last_response_time = None


async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        http_session = aiohttp.ClientSession(timeout=timeout)
    return http_session


async def check_site() -> tuple[bool, bool, float | None, str]:
    """
    Returns:
        is_healthy: whether the request succeeded with an HTTP 2xx/3xx
        is_slow: whether the response time exceeded the slow threshold
        elapsed: response time in seconds, if measured
        detail: human-readable result
    """
    session = await get_http_session()
    start = time.perf_counter()

    try:
        async with session.get(
            CHECK_URL,
            headers={"User-Agent": "scp-status-bot/1.0"}
        ) as resp:
            elapsed = time.perf_counter() - start
            is_healthy = 200 <= resp.status < 400
            is_slow = elapsed >= SLOW_RESPONSE_THRESHOLD_SECONDS

            detail = f"HTTP {resp.status} in {elapsed:.2f}s"
            return is_healthy, is_slow, elapsed, detail

    except Exception as e:
        elapsed = time.perf_counter() - start
        return False, False, elapsed, f"{type(e).__name__}: {e}"


def build_status_embed() -> discord.Embed:
    embed = discord.Embed(title="Wiki Status")
    embed.add_field(name="State", value=last_status, inline=False)
    embed.add_field(name="Last Result", value=last_detail, inline=False)

    if last_response_time is not None:
        embed.add_field(
            name="Last Response Time",
            value=f"{last_response_time:.2f}s",
            inline=False
        )

    embed.add_field(
        name="Failure Streak",
        value=f"{consecutive_failures}/{FAIL_THRESHOLD}",
        inline=False
    )
    return embed


def build_outage_embed(detail: str) -> discord.Embed:
    embed = discord.Embed(title="IT'S SO OVER")
    embed.description = f"The site failed {consecutive_failures} checks in a row."
    embed.add_field(name="Latest Result", value=detail, inline=False)
    embed.set_image(url="attachment://image.png")
    return embed


def build_recovery_embed(detail: str) -> discord.Embed:
    embed = discord.Embed(title="WE ARE SO BACK")
    embed.description = "The site appears to be back up!"
    embed.add_field(name="Latest Result", value=detail, inline=False)
    return embed


def build_slow_response_embed(detail: str, elapsed: float) -> discord.Embed:
    embed = discord.Embed(title="Wiki is responding slowly")
    embed.description = (
        f"The site responded, but it took {elapsed:.2f}s, "
        f"which is over the {SLOW_RESPONSE_THRESHOLD_SECONDS:.0f}s threshold."
    )
    embed.add_field(name="Latest Result", value=detail, inline=False)
    return embed


def build_slow_recovery_embed(detail: str, elapsed: float) -> discord.Embed:
    embed = discord.Embed(title="Wiki response time recovered")
    embed.description = (
        f"The site response time is back under the threshold at {elapsed:.2f}s."
    )
    embed.add_field(name="Latest Result", value=detail, inline=False)
    return embed


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def monitor_site():
    global consecutive_failures, outage_announced, slow_response_announced
    global last_status, last_detail, last_response_time

    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print("Channel not found. Check CHANNEL_ID and bot permissions.")
        return

    is_healthy, is_slow, elapsed, detail = await check_site()
    last_detail = detail
    last_response_time = elapsed

    if is_healthy:
        consecutive_failures = 0

        if is_slow:
            last_status = "UP BUT SLOW"
            print(f"[SLOW] {detail}")

            if outage_announced:
                await channel.send(embed=build_recovery_embed(detail))
                outage_announced = False

            if not slow_response_announced and elapsed is not None:
                await channel.send(embed=build_slow_response_embed(detail, elapsed))
                slow_response_announced = True

            return

        last_status = "UP"
        print(f"[OK] {detail}")

        if outage_announced:
            await channel.send(embed=build_recovery_embed(detail))
            outage_announced = False

        if slow_response_announced and elapsed is not None:
            await channel.send(embed=build_slow_recovery_embed(detail, elapsed))
            slow_response_announced = False

        return

    last_status = "DOWN/DEGRADED"
    consecutive_failures += 1
    slow_response_announced = False
    print(f"[FAIL {consecutive_failures}/{FAIL_THRESHOLD}] {detail}")

    if consecutive_failures >= FAIL_THRESHOLD and not outage_announced:
        file = discord.File("image.png", filename="image.png")
        embed = build_outage_embed(detail)
        await channel.send(embed=embed, file=file)
        outage_announced = True


@monitor_site.before_loop
async def before_monitor_site():
    await client.wait_until_ready()


@client.event
async def on_ready():
    print(f"Logged in as {client.user} ({client.user.id})")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    if not monitor_site.is_running():
        monitor_site.start()


@tree.command(name="status", description="Show the result of the last ping.")
async def status_command(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_status_embed())


@tree.command(name="forcecheck", description="Run a live check of the wiki's status right now.")
async def forcecheck_command(interaction: discord.Interaction):
    global consecutive_failures, outage_announced, slow_response_announced
    global last_status, last_detail, last_response_time

    await interaction.response.defer(thinking=True)

    is_healthy, is_slow, elapsed, detail = await check_site()
    last_detail = detail
    last_response_time = elapsed

    if is_healthy:
        consecutive_failures = 0

        if is_slow:
            last_status = "UP BUT SLOW"
            slow_response_announced = True
        else:
            last_status = "UP"
            slow_response_announced = False

        outage_announced = False
    else:
        last_status = "DOWN/DEGRADED"
        consecutive_failures += 1

    embed = discord.Embed(title="SCP Wiki Live Check")
    embed.add_field(name="State", value=last_status, inline=False)
    embed.add_field(name="Result", value=last_detail, inline=False)

    if last_response_time is not None:
        embed.add_field(
            name="Response Time",
            value=f"{last_response_time:.2f}s",
            inline=False
        )

    embed.add_field(
        name="Failure Streak",
        value=f"{consecutive_failures}/{FAIL_THRESHOLD}",
        inline=False
    )

    await interaction.followup.send(embed=embed)


@client.event
async def on_disconnect():
    print("Bot disconnected.")


async def close_http_session():
    global http_session
    if http_session and not http_session.closed:
        await http_session.close()


async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set.")
    if CHANNEL_ID == 0:
        raise RuntimeError("CHANNEL_ID is not set or invalid.")

    try:
        await client.start(DISCORD_TOKEN)
    finally:
        await close_http_session()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())