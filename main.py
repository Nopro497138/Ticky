# ticket_tool_like_bot.py
import os
import sqlite3
import asyncio
from datetime import datetime
from typing import Optional, List
import io

import discord
from discord import app_commands
from discord.ext import commands
from discord import ui
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID')) if os.getenv('GUILD_ID') else None
STAFF_ROLE_ID = int(os.getenv('STAFF_ROLE_ID')) if os.getenv('STAFF_ROLE_ID') else None
DB_PATH = os.getenv('DB_PATH', './tickets.sqlite')
POST_CHANNEL_ID = int(os.getenv('POST_CHANNEL_ID')) if os.getenv('POST_CHANNEL_ID') else None
TRANSCRIPT_CHANNEL_ID = int(os.getenv('TRANSCRIPT_CHANNEL_ID')) if os.getenv('TRANSCRIPT_CHANNEL_ID') else None
STAFF_ADD_LIMIT = int(os.getenv('STAFF_ADD_LIMIT', '20'))

DEFAULT_COLOR = 0x5865F2

if not DISCORD_TOKEN or not GUILD_ID:
    print('Please set DISCORD_TOKEN and GUILD_ID in your .env')
    raise SystemExit(1)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# --- Database setup ---
# make sqlite safe for async threads
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute('''
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT UNIQUE,
    channel_id TEXT,
    user_id TEXT,
    choice TEXT,
    created_at INTEGER,
    closed_at INTEGER,
    status TEXT,
    claimed_by TEXT
)
''')
conn.commit()

# --- Helpers ---


def now_ts() -> int:
    return int(datetime.utcnow().timestamp())


def ts_to_str(ts: int) -> str:
    return datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S UTC')


def make_embed(title: str, description: str, color: int = DEFAULT_COLOR) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.timestamp = datetime.utcnow()
    return e


def thread_safe_name(choice: str, username: str) -> str:
    base = (choice or 'ticket').lower()
    base = ''.join(ch for ch in base if ch.isalnum() or ch in '-_')[:12]
    user = ''.join(ch for ch in username.lower() if ch.isalnum())[:8] or 'u'
    rand = int.from_bytes(os.urandom(2), 'big') % 9000 + 1000
    return f"{base}-{user}-{rand}"


def is_staff(member: Optional[discord.Member]) -> bool:
    if member is None:
        return False
    if member.guild_permissions.administrator:
        return True
    if STAFF_ROLE_ID:
        role = member.guild.get_role(STAFF_ROLE_ID)
        if role and role in member.roles:
            return True
    return False


async def generate_transcript(thread: discord.Thread, include_attachments: bool = True) -> io.BytesIO:
    """
    Generate a simple text transcript for the given thread.
    Returns a BytesIO containing the transcript (UTF-8).
    """
    lines: List[str] = []
    header = f"Transcript for thread {thread.name} (id: {thread.id})\nChannel: {thread.parent.name if thread.parent else 'unknown'}\nCreated: {thread.created_at}\n\n"
    lines.append(header)
    # fetch messages oldest first
    async for m in thread.history(limit=None, oldest_first=True):
        t = m.created_at.strftime('%Y-%m-%d %H:%M:%S')
        author = f"{m.author} (id:{getattr(m.author, 'id', 'unknown')})"
        content = m.content or ''
        # add attachments info if requested
        if include_attachments and m.attachments:
            att_lines = []
            for a in m.attachments:
                att_lines.append(f"[Attachment] filename={a.filename} url={a.url} size={a.size}")
            content += ("\n" + "\n".join(att_lines))
        # system messages may not be simple, handle embeds briefly
        if m.embeds:
            content += "\n[Embeds present]"
        lines.append(f"[{t}] {author}: {content}\n")
    body = "\n".join(lines)
    bio = io.BytesIO(body.encode('utf-8'))
    bio.seek(0)
    return bio


# --- UI Components ---

class TicketSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label='Purchase Items', value='purchase', description='Buy any item in our market!', emoji='💰'),
            discord.SelectOption(label='Staff Help', value='staff', description='Reach staff about your questions and concerns!', emoji='⚙️'),
            discord.SelectOption(label='Other', value='other', description='All other questions or requests', emoji='❓')
        ]
        super().__init__(placeholder='Choose a reason for your ticket',
                         min_values=1, max_values=1, options=options,
                         custom_id='ticket_select_v1')

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        choice = self.values[0]
        user = interaction.user
        channel = interaction.channel

        if not channel or not isinstance(channel, discord.TextChannel):
            embed = make_embed('Error', 'Channel nicht gefunden oder ist kein TextChannel.')
            return await interaction.followup.send(embed=embed, ephemeral=True)

        bot_member = interaction.guild.me if interaction.guild else None
        perms = channel.permissions_for(bot_member) if bot_member else channel.permissions_for(interaction.guild.get_member(bot.user.id))
        if not perms.create_private_threads:
            embed = make_embed('Permission error', 'Ich habe nicht die nötigen Rechte, um private Threads in diesem Kanal zu erstellen.')
            return await interaction.followup.send(embed=embed, ephemeral=True)

        thread_name = thread_safe_name(choice, user.name)
        try:
            thread = await channel.create_thread(name=thread_name, type=discord.ChannelType.private_thread, auto_archive_duration=1440)
        except Exception as e:
            embed = make_embed('Error creating thread', f'Fehler beim Erstellen des Threads:\n```\n{e}\n```')
            return await interaction.followup.send(embed=embed, ephemeral=True)

        # add creator
        try:
            await thread.add_user(user)
        except Exception:
            pass

        # optionally add some staff
        fallback_role_mention = False
        if STAFF_ROLE_ID:
            role = interaction.guild.get_role(STAFF_ROLE_ID)
            if role:
                members = [m for m in role.members]
                if members:
                    to_add = members[:STAFF_ADD_LIMIT]
                    for m in to_add:
                        try:
                            await thread.add_user(m)
                            await asyncio.sleep(0.25)
                        except Exception:
                            continue
                    if len(members) > STAFF_ADD_LIMIT:
                        fallback_role_mention = True

        # save ticket
        try:
            cur = conn.cursor()
            cur.execute('INSERT OR IGNORE INTO tickets (thread_id, channel_id, user_id, choice, created_at, status) VALUES (?, ?, ?, ?, ?, ?)',
                        (str(thread.id), str(channel.id), str(user.id), choice, now_ts(), 'open'))
            conn.commit()
        except Exception:
            pass

        # send welcome message with persistent thread controls
        v = TicketThreadView()  # non-persistent; the persistent view is added on_ready for global handlers
        human = {'purchase': 'Purchase Items', 'staff': 'Staff Help', 'other': 'Other'}.get(choice, choice)
        description = f'Hallo {user.mention}, danke für dein Ticket ({human}). Ein Staff-Mitglied wird sich bald melden.'
        if fallback_role_mention and STAFF_ROLE_ID:
            description += f'\n\nHinweis: Viele Staff-Mitglieder vorhanden — Rolle wird getagged: <@&{STAFF_ROLE_ID}>'

        thread_embed = make_embed('Neues Ticket', description)
        try:
            await thread.send(embed=thread_embed, view=v)
        except Exception:
            pass

        confirm_embed = make_embed('Ticket erstellt', f'Dein Ticket wurde erstellt: {thread.mention}')
        return await interaction.followup.send(embed=confirm_embed, ephemeral=True)


class TicketSelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


# Thread-level controls: we create button classes but also register a generic persistent view (see on_ready)
class CloseButton(ui.Button):
    def __init__(self):
        super().__init__(label='Close', style=discord.ButtonStyle.danger, custom_id='ticket_close_v1')

    async def callback(self, interaction: discord.Interaction):
        await handle_close(interaction, reason=None)


class ClaimButton(ui.Button):
    def __init__(self):
        super().__init__(label='Claim', style=discord.ButtonStyle.secondary, custom_id='ticket_claim_v1')

    async def callback(self, interaction: discord.Interaction):
        await handle_claim(interaction)


class TranscriptButton(ui.Button):
    def __init__(self):
        super().__init__(label='Transcript', style=discord.ButtonStyle.primary, custom_id='ticket_transcript_v1')

    async def callback(self, interaction: discord.Interaction):
        await handle_transcript(interaction)


class LockButton(ui.Button):
    def __init__(self):
        super().__init__(label='Lock', style=discord.ButtonStyle.secondary, custom_id='ticket_lock_v1')

    async def callback(self, interaction: discord.Interaction):
        await handle_lock_toggle(interaction)


class TicketThreadView(ui.View):
    """This view is attached to the initial thread message (non-persistent instance for the message send)"""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CloseButton())
        self.add_item(ClaimButton())
        self.add_item(TranscriptButton())
        self.add_item(LockButton())


# --- Command Handlers (helper functions used by buttons and commands) ---


async def handle_claim(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.response.send_message(embed=make_embed('Invalid context', 'Dieses Kommando funktioniert nur in Ticket-Threads.'), ephemeral=True)

    cur = conn.cursor()
    cur.execute('SELECT * FROM tickets WHERE thread_id = ?', (str(channel.id),))
    row = cur.fetchone()
    if not row:
        return await interaction.response.send_message(embed=make_embed('Not a ticket', 'Dieses Thread ist kein bekanntes Ticket.'), ephemeral=True)

    member = interaction.user
    if not is_staff(member):
        return await interaction.response.send_message(embed=make_embed('Permission denied', 'Nur Staff kann Tickets claimen.'), ephemeral=True)

    # update DB
    try:
        cur.execute('UPDATE tickets SET claimed_by = ? WHERE thread_id = ?', (str(member.id), str(channel.id)))
        conn.commit()
    except Exception:
        pass

    # send message into thread
    try:
        await channel.send(f'✅ Ticket claimed by {member.mention}')
    except Exception:
        pass

    return await interaction.response.send_message(embed=make_embed('Ticket claimed', f'{member.mention} hat das Ticket übernommen.'), ephemeral=True)


async def handle_close(interaction: discord.Interaction, reason: Optional[str]):
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.response.send_message(embed=make_embed('Invalid context', 'Dieses Kommando funktioniert nur in Ticket-Threads.'), ephemeral=True)

    cur = conn.cursor()
    cur.execute('SELECT * FROM tickets WHERE thread_id = ?', (str(channel.id),))
    row = cur.fetchone()
    ticket_owner_id = int(row['user_id']) if row else None

    member = interaction.user
    if not (is_staff(member) or (ticket_owner_id == getattr(member, 'id', None))):
        return await interaction.response.send_message(embed=make_embed('Permission denied', 'Nur Ticket-Ersteller oder Staff können das Ticket schließen.'), ephemeral=True)

    # archive thread and update DB
    try:
        await channel.edit(archived=True)
        cur.execute('UPDATE tickets SET status = ?, closed_at = ? WHERE thread_id = ?', ('closed', now_ts(), str(channel.id)))
        conn.commit()
    except Exception as e:
        return await interaction.response.send_message(embed=make_embed('Error', f'Fehler beim Archivieren: {e}'), ephemeral=True)

    # optionally generate transcript and post it
    try:
        bio = await generate_transcript(channel)
        filename = f"transcript-{channel.name}-{channel.id}.txt"
        discord_file = discord.File(fp=bio, filename=filename)
        if TRANSCRIPT_CHANNEL_ID:
            try:
                log_chan = channel.guild.get_channel(TRANSCRIPT_CHANNEL_ID) or await channel.guild.fetch_channel(TRANSCRIPT_CHANNEL_ID)
                if isinstance(log_chan, discord.TextChannel):
                    await log_chan.send(content=f"📜 Transcript for ticket {channel.name} (id:{channel.id})", file=discord_file)
            except Exception:
                # if upload fails, try to DM the command invoker
                try:
                    await interaction.user.send(embed=make_embed('Transcript failed to post', 'Konnte Transcript nicht in den Kanal posten. Hier ist eine Kopie.'), file=discord_file)
                except Exception:
                    pass
        else:
            # send transcript to the user as DM (best-effort)
            try:
                await interaction.user.send(embed=make_embed('Ticket transcript', f'Hier ist das Transcript für {channel.name}'), file=discord_file)
            except Exception:
                pass
    except Exception:
        pass

    try:
        await channel.send(embed=make_embed('Ticket geschlossen', 'Das Ticket wurde geschlossen und archiviert.'))
    except Exception:
        pass

    return await interaction.response.send_message(embed=make_embed('Closed', 'Ticket geschlossen.'), ephemeral=True)


async def handle_transcript(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.response.send_message(embed=make_embed('Invalid context', 'Dieses Kommando funktioniert nur in Ticket-Threads.'), ephemeral=True)

    cur = conn.cursor()
    cur.execute('SELECT * FROM tickets WHERE thread_id = ?', (str(channel.id),))
    row = cur.fetchone()
    if not row:
        return await interaction.response.send_message(embed=make_embed('Not a ticket', 'Dieses Thread ist kein bekanntes Ticket.'), ephemeral=True)

    if not is_staff(interaction.user) and int(row['user_id']) != interaction.user.id:
        return await interaction.response.send_message(embed=make_embed('Permission denied', 'Nur Ticket-Ersteller oder Staff können Transcripts anfordern.'), ephemeral=True)

    try:
        bio = await generate_transcript(channel)
        filename = f"transcript-{channel.name}-{channel.id}.txt"
        discord_file = discord.File(fp=bio, filename=filename)
        # If TRANSCRIPT channel configured, post there; also DM user
        posted = False
        if TRANSCRIPT_CHANNEL_ID:
            try:
                log_chan = channel.guild.get_channel(TRANSCRIPT_CHANNEL_ID) or await channel.guild.fetch_channel(TRANSCRIPT_CHANNEL_ID)
                if isinstance(log_chan, discord.TextChannel):
                    await log_chan.send(content=f"📜 Transcript for ticket {channel.name} (id:{channel.id})", file=discord_file)
                    posted = True
            except Exception:
                posted = False
        if not posted:
            # send as DM to requester
            try:
                await interaction.user.send(embed=make_embed('Ticket transcript', f'Hier ist das Transcript für {channel.name}'), file=discord_file)
                return await interaction.response.send_message(embed=make_embed('Sent', 'Transcript wurde per DM gesendet.'), ephemeral=True)
            except Exception:
                return await interaction.response.send_message(embed=make_embed('Failed', 'Konnte Transcript nicht senden.'), ephemeral=True)
        else:
            return await interaction.response.send_message(embed=make_embed('Posted', f'Transcript gepostet in <#{TRANSCRIPT_CHANNEL_ID}>.'), ephemeral=True)
    except Exception as e:
        return await interaction.response.send_message(embed=make_embed('Error', f'Fehler beim Erstellen des Transcripts: {e}'), ephemeral=True)


async def handle_lock_toggle(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.response.send_message(embed=make_embed('Invalid context', 'Dieses Kommando funktioniert nur in Ticket-Threads.'), ephemeral=True)

    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=make_embed('Permission denied', 'Nur Staff kann Tickets sperren/entsperren.'), ephemeral=True)

    try:
        # Toggle lock
        new_locked = not getattr(channel, 'locked', False)
        await channel.edit(locked=new_locked)
        status = 'gesperrt' if new_locked else 'entsperrt'
        await channel.send(f'🔒 Ticket {status} by {interaction.user.mention}')
        return await interaction.response.send_message(embed=make_embed('Done', f'Ticket {status}.'), ephemeral=True)
    except Exception as e:
        return await interaction.response.send_message(embed=make_embed('Error', f'Fehler beim Ändern der Sperre: {e}'), ephemeral=True)


# --- Slash Commands ---

@bot.tree.command(name='ticket_setup', description='Post the ticket dropdown menu to a channel')
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(channel='Channel to post the ticket select menu into')
async def ticket_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message(embed=make_embed('Wrong Guild', 'Dieser Command ist nur in der konfigurierten Guild erlaubt.'), ephemeral=True)

    bot_member = interaction.guild.me if interaction.guild else None
    perms = channel.permissions_for(bot_member) if bot_member else channel.permissions_for(interaction.guild.get_member(bot.user.id))
    if not (perms.send_messages and perms.create_private_threads and perms.read_message_history):
        return await interaction.response.send_message(embed=make_embed('Missing Permissions', 'Ich benötige: Send Messages, Create Private Threads, Read Message History in diesem Kanal.'), ephemeral=True)

    embed = make_embed('Make a selection', 'Wähle die passende Option um ein Ticket zu öffnen.')
    view = TicketSelectView()
    try:
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(embed=make_embed('Posted', f'Ticket Menü in {channel.mention} gepostet.'), ephemeral=True)
    except Exception as e:
        return await interaction.response.send_message(embed=make_embed('Error posting menu', f'Fehler:\n```\n{e}\n```'), ephemeral=True)


@bot.tree.command(name='ticket_close', description='Close the current ticket (thread).')
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def cmd_ticket_close(interaction: discord.Interaction, reason: Optional[str] = None):
    # wrapper to call handle_close
    await handle_close(interaction, reason)


@bot.tree.command(name='ticket_claim', description='Claim this ticket as staff.')
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def cmd_ticket_claim(interaction: discord.Interaction):
    await handle_claim(interaction)


@bot.tree.command(name='ticket_transcript', description='Generate/send transcript for this ticket.')
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def cmd_ticket_transcript(interaction: discord.Interaction):
    await handle_transcript(interaction)


@bot.tree.command(name='ticket_add', description='Add a member to the ticket thread (staff only).')
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(member='Member to add to the ticket thread')
async def cmd_ticket_add(interaction: discord.Interaction, member: discord.Member):
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.response.send_message(embed=make_embed('Invalid context', 'Nur in Ticket-Threads verwendbar.'), ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=make_embed('Permission denied', 'Nur Staff kann Mitglieder hinzufügen.'), ephemeral=True)
    try:
        await channel.add_user(member)
        return await interaction.response.send_message(embed=make_embed('Added', f'{member.mention} wurde dem Ticket hinzugefügt.'), ephemeral=True)
    except Exception as e:
        return await interaction.response.send_message(embed=make_embed('Error', f'Fehler: {e}'), ephemeral=True)


@bot.tree.command(name='ticket_remove', description='Remove a member from the ticket thread (staff only).')
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(member='Member to remove from the ticket thread')
async def cmd_ticket_remove(interaction: discord.Interaction, member: discord.Member):
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.response.send_message(embed=make_embed('Invalid context', 'Nur in Ticket-Threads verwendbar.'), ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.response.send_message(embed=make_embed('Permission denied', 'Nur Staff kann Mitglieder entfernen.'), ephemeral=True)
    try:
        await channel.remove_user(member)
        return await interaction.response.send_message(embed=make_embed('Removed', f'{member.mention} wurde entfernt.'), ephemeral=True)
    except Exception as e:
        return await interaction.response.send_message(embed=make_embed('Error', f'Fehler: {e}'), ephemeral=True)


@bot.tree.command(name='ticket_lock', description='Lock the ticket (staff only).')
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def cmd_ticket_lock(interaction: discord.Interaction):
    await handle_lock_toggle(interaction)


# --- on_ready: sync commands & register persistent views ---


@bot.event
async def on_ready():
    print('Logged in as', bot.user, '— id:', bot.user.id)
    # register the persistent select view (the menu)
    try:
        bot.add_view(TicketSelectView())
        # register persistent thread-action view which contains button handlers (no per-thread state stored in view)
        persistent_thread_view = ui.View(timeout=None)
        persistent_thread_view.add_item(CloseButton())
        persistent_thread_view.add_item(ClaimButton())
        persistent_thread_view.add_item(TranscriptButton())
        persistent_thread_view.add_item(LockButton())
        bot.add_view(persistent_thread_view)
    except Exception as e:
        print('Could not add persistent views:', e)

    # sync commands to the guild for faster iteration
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print('Command tree synced to guild', GUILD_ID)
    except Exception as e:
        print('Command sync failed (guild). Trying global sync:', e)
        try:
            await bot.tree.sync()
            print('Global sync succeeded')
        except Exception as ge:
            print('Global sync failed as well:', ge)

    try:
        print('Registered app commands:', bot.tree.commands)
    except Exception:
        pass

    # optional auto-post
    if POST_CHANNEL_ID:
        try:
            ch = bot.get_channel(POST_CHANNEL_ID) or await bot.fetch_channel(POST_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                embed = make_embed('Make a selection', 'Choose the appropriate option to open a ticket.')
                await ch.send(embed=embed, view=TicketSelectView())
                print('Posted ticket menu automatically in', ch.id)
        except Exception as e:
            print('Could not auto-post menu:', e)


# --- Run ---
if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
