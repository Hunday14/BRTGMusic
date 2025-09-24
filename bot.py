# -*- coding: utf-8 -*-
import discord
from discord.ext import commands, tasks
import asyncio
from yt_dlp import YoutubeDL
from youtubesearchpython import VideosSearch
import re

# ---------- KONFIG ----------
TOKEN = "MTQyMDMyNDE4NzEyOTc3NDE1NA.GZbtgr.2I9U2D17CT1lRuTVr6iEK4C3nebBScfNCWJSWA"  # <--- ide másold a bot tokened
PREFIX = "!"
INTENTS = discord.Intents.default()
INTENTS.message_content = True
# ---------------------------

bot = commands.Bot(command_prefix=PREFIX, intents=INTENTS)

# FFmpeg + yt-dlp opciók
YTDL_OPTS = {
    "format": "bestaudio",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0"
}
FFMPEG_OPTS = {
    "options": "-vn"
}

ytdl = YoutubeDL(YTDL_OPTS)

# Regex a YouTube link felismeréséhez (rövid és hosszú)
YOUTUBE_URL_REGEX = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/")

# Per-guild zenei állapot tárolása
class GuildMusic:
    def __init__(self):
        self.queue = []             # lista: (webstream_url, title, original_query)
        self.history = []           # lejátszottak listája (title, url)
        self.current = None         # (title, url)
        self.voice_client = None    # discord.VoiceClient
        self.playing = False
        self.autoplay = False
        self.control_message = None # az az üzenet, amelyre reakciók kerülnek
        self.lock = asyncio.Lock()  # a queue kezelése közben

    def enqueue(self, entry):
        self.queue.append(entry)

    def enqueue_front(self, entry):
        self.queue.insert(0, entry)

    def has_next(self):
        return len(self.queue) > 0

guild_music = {}  # guild_id -> GuildMusic

def get_gm(ctx):
    gid = ctx.guild.id
    if gid not in guild_music:
        guild_music[gid] = GuildMusic()
    return guild_music[gid]

# ---------- Segédfüggvények ----------
async def ensure_voice(ctx, gm: GuildMusic):
    """Csatlakozik a felhasználó hangcsatornájához, ha még nincs voice client."""
    if ctx.author.voice is None:
        await ctx.send("Először csatlakozz egy voice channelhez!")
        return False

    channel = ctx.author.voice.channel
    if gm.voice_client is None or not gm.voice_client.is_connected():
        gm.voice_client = await channel.connect()
    else:
        # ha más csatornában van, csatlakozz át
        if gm.voice_client.channel != channel:
            await gm.voice_client.move_to(channel)
    return True

def is_url(query: str) -> bool:
    return bool(YOUTUBE_URL_REGEX.search(query))

def ytdl_extract(query: str):
    """yt-dlp-val kinyeri a stream URL-t és a címet. query lehet link vagy keresőkifejezés."""
    info = ytdl.extract_info(query, download=False)
    # Ha playlistet ad vissza, vegyük az első elemet
    if "entries" in info:
        info = info["entries"][0]
    # 'url' mező sokszor a közvetlen stream URL
    stream_url = info.get("url")
    title = info.get("title")
    webpage_url = info.get("webpage_url") or query
    return stream_url, title, webpage_url

async def search_youtube_first(query: str):
    """YouTube keresés: első találat linkje és címe."""
    vs = VideosSearch(query, limit=1)
    res = vs.result().get("result")
    if not res:
        return None, None
    first = res[0]
    return first.get("link"), first.get("title")

async def build_control_message_embed(gm: GuildMusic):
    embed = discord.Embed(title="🎶 Music Bot vezérlő", colour=discord.Colour.blurple())
    now = gm.current[0] if gm.current else "—"
    embed.add_field(name="Most játszódik", value=now, inline=False)
    q = "\n".join([f"{i+1}. {item[1]}" for i, item in enumerate(gm.queue[:10])]) or "Üres"
    embed.add_field(name=f"Queue ({len(gm.queue)})", value=q, inline=False)
    embed.set_footer(text=f"Autoplay: {'ON' if gm.autoplay else 'OFF'}")
    return embed

async def send_or_update_control_message(ctx, gm: GuildMusic):
    embed = await build_control_message_embed(gm)
    if gm.control_message and not gm.control_message.deleted:
        try:
            await gm.control_message.edit(embed=embed)
            return gm.control_message
        except Exception:
            gm.control_message = None
    # küldünk újat
    msg = await ctx.send(embed=embed)
    gm.control_message = msg
    # reakciók hozzáadása egyszer
    try:
        for r in ("⏯", "⏭", "⏮", "⏹", "🔁"):
            await msg.add_reaction(r)
    except Exception:
        pass
    return msg

async def play_next_track(ctx, gm: GuildMusic):
    """Lejátssza a következő tracket a queue-ból. Visszahívódik after-ban."""
    async with gm.lock:
        if gm.voice_client is None:
            return
        if gm.voice_client.is_playing():
            return

        # ha nincs a queue-ban semmi -> autoplay logika
        if not gm.has_next():
            if gm.autoplay and gm.current:
                # egyszerű autoplay: keresünk hasonló (a jelenlegi cím + 'related' keresés)
                base_title = gm.current[0]
                query = base_title + " related"
                link, title = await search_youtube_first(query)
                if link:
                    try:
                        stream_url, real_title, web_url = ytdl_extract(link)
                        gm.enqueue((stream_url, real_title, web_url))
                    except Exception:
                        pass

        if not gm.has_next():
            gm.playing = False
            # frissítjük a kontroll üzenetet, ha van
            try:
                if gm.control_message:
                    await send_or_update_control_message(ctx, gm)
            except Exception:
                pass
            return

        # vegyük ki a következőt
        stream_url, title, web_url = gm.queue.pop(0)
        gm.current = (title, web_url)
        gm.history.append((title, web_url))
        gm.playing = True

        # létrehozzuk a ffmpeg forrást és lejátsszuk
        try:
            source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTS)
            player = discord.PCMVolumeTransformer(source, volume=0.9)
            def after_play(error):
                # az after hívás szinkron threadből jön -> schedule coroutine
                coro = play_next_track(ctx, gm)
                fut = asyncio.run_coroutine_threadsafe(coro, bot.loop)
                try:
                    fut.result()
                except Exception:
                    pass

            gm.voice_client.play(player, after=after_play)
            # frissítjük a kontroll üzenetet
            try:
                if gm.control_message:
                    await send_or_update_control_message(ctx, gm)
            except Exception:
                pass
        except Exception as e:
            await ctx.send(f"Hiba a lejátszásnál: {e}")
            # próbáljuk a következőt
            await play_next_track(ctx, gm)

# ---------- Parancsok ----------
@bot.event
async def on_ready():
    print(f"Bejelentkezve: {bot.user} (id: {bot.user.id})")

@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query: str):
    """!play <link vagy keresőkifejezés> - linkkel pontos, névvel az első találatot játssza"""
    gm = get_gm(ctx)
    if not await ensure_voice(ctx, gm):
        return

    await ctx.trigger_typing()

    # ha link
    try:
        if is_url(query):
            # direkt linket kapunk
            stream_url, title, web_url = ytdl_extract(query)
        else:
            # keresés -> első találat
            link, title = await search_youtube_first(query)
            if not link:
                await ctx.send("Nem találtam semmit a keresésre.")
                return
            stream_url, title, web_url = ytdl_extract(link)
    except Exception as e:
        await ctx.send(f"Hiba a YouTube lekérésnél: {e}")
        return

    # betesszük a queue-ba
    async with gm.lock:
        gm.enqueue((stream_url, title, web_url))

    await ctx.send(f"✅ Hozzáadva a queue-hoz: **{title}**")
    # ha nincs most semmi lejátszás alatt, indítsuk el
    if not gm.playing:
        await play_next_track(ctx, gm)

    # kontroll üzenet és reakciók
    await send_or_update_control_message(ctx, gm)

@bot.command(name="queue", aliases=["q"])
async def show_queue(ctx):
    gm = get_gm(ctx)
    embed = await build_control_message_embed(gm)
    await ctx.send(embed=embed)

@bot.command(name="skip", aliases=["s"])
async def skip(ctx):
    gm = get_gm(ctx)
    if gm.voice_client and gm.voice_client.is_playing():
        gm.voice_client.stop()  # after callback hívja a következőt
        await ctx.send("⏭ Kihagyva.")
    else:
        await ctx.send("Nincs mit kihagyni.")

@bot.command(name="pause")
async def pause(ctx):
    gm = get_gm(ctx)
    if gm.voice_client and gm.voice_client.is_playing():
        gm.voice_client.pause()
        await ctx.send("⏸ Leállítva.")
    else:
        await ctx.send("Nem megy semmi.")

@bot.command(name="resume")
async def resume(ctx):
    gm = get_gm(ctx)
    if gm.voice_client and gm.voice_client.is_paused():
        gm.voice_client.resume()
        await ctx.send("▶ Folytatva.")
    else:
        await ctx.send("Nincs szüneteltetett zenéd.")

@bot.command(name="stop")
async def stop(ctx):
    gm = get_gm(ctx)
    if gm.voice_client:
        gm.queue.clear()
        gm.playing = False
        gm.current = None
        gm.voice_client.stop()
        try:
            await gm.voice_client.disconnect()
        except Exception:
            pass
        gm.voice_client = None
        await ctx.send("⏹ Leállítva és bontva a csatlakozás.")
    else:
        await ctx.send("A bot nincs csatlakozva.")

@bot.command(name="autoplay")
async def toggle_autoplay(ctx):
    gm = get_gm(ctx)
    gm.autoplay = not gm.autoplay
    await ctx.send(f"🔁 Autoplay {'bekapcsolva' if gm.autoplay else 'kikapcsolva'}.")
    if gm.control_message:
        await send_or_update_control_message(ctx, gm)

@bot.command(name="previous", aliases=["prev"])
async def previous(ctx):
    gm = get_gm(ctx)
    if len(gm.history) >= 2:
        # az aktuális a history utolsó, az előző a -2
        prev_title, prev_web = gm.history[-2]
        try:
            stream_url, title, web_url = ytdl_extract(prev_web)
            # ahogy kérted: hátra lépés -> előre játsszuk a korábbi tracket (ráhelyezzük a queue elé)
            async with gm.lock:
                gm.enqueue_front((stream_url, title, web_url))
            # stop-olni fogjuk a mostanit -> next meghívódik és előkerül a korábbi
            if gm.voice_client and gm.voice_client.is_playing():
                gm.voice_client.stop()
            await ctx.send(f"⏮ Visszaléptem: **{title}**")
        except Exception as e:
            await ctx.send(f"Hiba: {e}")
    else:
        await ctx.send("Nincs előző szám a historyban.")

# ---------- Reakciókezelés (emoji gombok) ----------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # figyeljük a kontroll üzenetre érkező reakciókat
    if payload.user_id == bot.user.id:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    gm = guild_music.get(guild.id)
    if not gm or not gm.control_message:
        return
    if payload.message_id != gm.control_message.id:
        return

    member = guild.get_member(payload.user_id)
    if not member:
        return

    emoji = str(payload.emoji)
    ctx = None
    # próbáljuk meg megszerezni a kontextust egy egyszerű dummy ctx-hez
    channel = guild.get_channel(payload.channel_id)
    # parancsok meghívása a reakció alapján
    try:
        if emoji == "⏯":
            # váltás pause/resume
            if gm.voice_client and gm.voice_client.is_playing():
                gm.voice_client.pause()
                await channel.send("⏸ Leállítva (reakció).")
            elif gm.voice_client and gm.voice_client.is_paused():
                gm.voice_client.resume()
                await channel.send("▶ Folytatva (reakció).")
            else:
                await channel.send("Nincs lejátszás.")
        elif emoji == "⏭":
            if gm.voice_client and (gm.voice_client.is_playing() or gm.voice_client.is_paused()):
                gm.voice_client.stop()
                await channel.send("⏭ Kihagyva (reakció).")
            else:
                await channel.send("Nincs mit kihagyni.")
        elif emoji == "⏮":
            # previous - ha van history
            if len(gm.history) >= 2:
                prev_title, prev_web = gm.history[-2]
                stream_url, title, web_url = ytdl_extract(prev_web)
                async with gm.lock:
                    gm.enqueue_front((stream_url, title, web_url))
                if gm.voice_client and gm.voice_client.is_playing():
                    gm.voice_client.stop()
                await channel.send(f"⏮ Visszaléptem (reakció): **{title}**")
            else:
                await channel.send("Nincs előző szám.")
        elif emoji == "⏹":
            # stop + disconnect
            if gm.voice_client:
                gm.queue.clear()
                gm.playing = False
                gm.current = None
                try:
                    gm.voice_client.stop()
                    await gm.voice_client.disconnect()
                except Exception:
                    pass
                gm.voice_client = None
                await channel.send("⏹ Leállítva és bontva (reakció).")
            else:
                await channel.send("A bot nincs csatlakozva.")
        elif emoji == "🔁":
            gm.autoplay = not gm.autoplay
            await channel.send(f"🔁 Autoplay {'bekapcsolva' if gm.autoplay else 'kikapcsolva'} (reakció).")
        # frissítsük a kontroll üzenetet
        try:
            await send_or_update_control_message(channel, gm)
        except Exception:
            pass
    except Exception as e:
        try:
            await channel.send(f"Reakciókezelési hiba: {e}")
        except Exception:
            pass
    finally:
        # próbáljuk törölni a felhasználó reakcióját, hogy újra tudja nyomni
        try:
            channel_obj = guild.get_channel(payload.channel_id)
            msg = await channel_obj.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, member)
        except Exception:
            pass

# ---------- Hibakezelés egyszerűen ----------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    await ctx.send(f"Hiba: {error}")

# ---------- Indítás ----------
if __name__ == "__main__":
    bot.run(TOKEN)
