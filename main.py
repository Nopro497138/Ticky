# ticket_tool_like_bot_en.py
import os
import sqlite3
import asyncio
from datetime import datetime, timezone
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
# optional initial transcript channel (can be overridden via admin panel)
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
c.execute('''
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
)
''')
conn.commit()

# set initial TRANSCRIPT channel in config if provided via env
if TRANSCRIPT_CHANNEL_ID:
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', ('transcript_channel_id', str(TRANSCRIPT_CHANNEL_ID)))
    conn.commit()

# --- Helpers ---

def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def ts_to_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

def make_embed(title: str, description: str, color: int = DEFAULT_COLOR) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.timestamp = datetime.now(timezone.utc)
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

def get_config(key: str) -> Optional[str]:
    cur = conn.cursor()
    cur.execute('SELECT value FROM config WHERE key = ?', (key,))
    row = cur.fetchone()
    return row['value'] if row else None

def set_config(key: str, value: str):
    cur = conn.cursor()
    cur.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, value))
    conn.commit()

async def generate_transcript(thread: discord.Thread, include_attachments: bool = True) -> io.BytesIO:
    """
    Generate a simple text transcript for the given thread.
    Returns a BytesIO containing the transcript (UTF-8).
    """
    lines: List[str] = []
    created = thread.created_at.isoformat() if thread.created_at else 'unknown'
    header = f"Transcript for thread {thread.name} (id: {thread.id})\nParent channel: {thread.parent.name if thread.parent else 'unknown'}\nCreated: {created}\n\n"
    lines.append(header)
    async for m in thread.history(limit=None, oldest_first=True):
        t = m.created_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S') if m.created_at else 'unknown'
        author = f"{m.author} (id:{getattr(m.author, 'id', 'unknown')})"
        content = m.content or ''
        if include_attachments and m.attachments:
            att_lines = []
            for a in m.attachments:
                att_lines.append(f"[Attachment] filename={a.filename} url={a.url} size={a.size}")
            content += ("\n" + "\n".join(att_lines))
        if m.embeds:
            content += "\n[Embeds present]"
        lines.append(f"[{t}] {author}: {content}\n")
    body = "\n".join(lines)
    bio = io.BytesIO(body.encode('utf-8'))
    bio.seek(0)
    return bio

# --- UI Components (English) ---

class TicketSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label='Purchase Items', value='purchase', description='Buy any item in our market!', emoji='💰'),
            discord.SelectOption(label='Staff Help', value='staff', description='Reach staff about your questions and concerns!', emoji='⚙️'),
            discord.SelectOption(label='Other', value='other', description='All other questions or requests', emoji='❓')
        ]
        super().__init__(placeholder='Choose a reason for your ticket',
                         min_values=1, max_values=1, options=options,
                         custom_id='ticket_select_v2')

    async def callback(self, interaction: discord.Interaction):
        # Defer early to avoid Unknown interaction when creating threads etc.
        if not interaction.response.is_done:
            await interaction.response.defer(ephemeral=True)
        choice = self.values[0]
        user = interaction.user
        channel = interaction.channel

        if not channel or not isinstance(channel, discord.TextChannel):
            embed = make_embed('Error', 'Channel not found or not a text channel.')
            return await interaction.followup.send(embed=embed, ephemeral=True)

        bot_member = interaction.guild.me if interaction.guild else None
        perms = channel.permissions_for(bot_member) if bot_member else channel.permissions_for(interaction.guild.get_member(bot.user.id))
        if not perms.create_private_threads:
            embed = make_embed('Permission error', "I don't have permission to create private threads in that channel.")
            return await interaction.followup.send(embed=embed, ephemeral=True)

        thread_name = thread_safe_name(choice, user.name)
        try:
            thread = await channel.create_thread(name=thread_name, type=discord.ChannelType.private_thread, auto_archive_duration=1440)
        except Exception as e:
            embed = make_embed('Error creating thread', f'An error occurred while creating the thread:\n```\n{e}\n```')
            return await interaction.followup.send(embed=embed, ephemeral=True)

        # add creator
        try:
            await thread.add_user(user)
        except Exception:
            pass

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

        try:
            cur = conn.cursor()
            cur.execute('INSERT OR IGNORE INTO tickets (thread_id, channel_id, user_id, choice, created_at, status) VALUES (?, ?, ?, ?, ?, ?)',
                        (str(thread.id), str(channel.id), str(user.id), choice, now_ts(), 'open'))
            conn.commit()
        except Exception:
            pass

        thread_view = TicketThreadView()
        human = {'purchase': 'Purchase Items', 'staff': 'Staff Help', 'other': 'Other'}.get(choice, choice)
        description = f'Hello {user.mention}, thanks for your ticket ({human}). A staff member will respond shortly.'
        if fallback_role_mention and STAFF_ROLE_ID:
            description += f'\n\nNote: many staff members detected — pinging role: <@&{STAFF_ROLE_ID}>'

        thread_embed = make_embed('New Ticket', description)
        try:
            await thread.send(embed=thread_embed, view=thread_view)
        except Exception:
            pass

        confirm_embed = make_embed('Ticket Created', f'Your ticket has been created: {thread.mention}')
        return await interaction.followup.send(embed=confirm_embed, ephemeral=True)

class TicketSelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())

class CloseButton(ui.Button):
    def __init__(self):
        super().__init__(label='Close', style=discord.ButtonStyle.danger, custom_id='ticket_close_v2')

    async def callback(self, interaction: discord.Interaction):
        await handle_close(interaction, reason=None)

class ClaimButton(ui.Button):
    def __init__(self):
        super().__init__(label='Claim', style=discord.ButtonStyle.secondary, custom_id='ticket_claim_v2')

    async def callback(self, interaction: discord.Interaction):
        await handle_claim(interaction)

class TranscriptButton(ui.Button):
    def __init__(self):
        super().__init__(label='Transcript', style=discord.ButtonStyle.primary, custom_id='ticket_transcript_v2')

    async def callback(self, interaction: discord.Interaction):
        await handle_transcript(interaction)

class LockButton(ui.Button):
    def __init__(self):
        super().__init__(label='Lock/Unlock', style=discord.ButtonStyle.secondary, custom_id='ticket_lock_v2')

    async def callback(self, interaction: discord.Interaction):
        await handle_lock_toggle(interaction)

class AdminButton_Delete(ui.Button):
    def __init__(self):
        super().__init__(label='Delete Thread', style=discord.ButtonStyle.danger, custom_id='admin_delete_thread_v1')

    async def callback(self, interaction: discord.Interaction):
        await admin_delete_flow(interaction)

class AdminButton_SendTranscript(ui.Button):
    def __init__(self):
        super().__init__(label='Send Transcript (choose channel)', style=discord.ButtonStyle.primary, custom_id='admin_send_transcript_v1')

    async def callback(self, interaction: discord.Interaction):
        # open a modal to ask for channel mention or ID
        if not interaction.response.is_done:
            await interaction.response.defer(ephemeral=True)
        thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
        if thread is None:
            return await interaction.followup.send(embed=make_embed('Invalid context', 'This command must be used inside a ticket thread.'), ephemeral=True)
        modal = ChannelModal(title='Send transcript to channel', thread=thread, action='send')
        return await interaction.followup.send(embed=make_embed('Modal opened', 'Check your client — a modal should open.'), ephemeral=True) or await interaction.response.send_modal(modal)

class AdminButton_SetDefaultTranscript(ui.Button):
    def __init__(self):
        super().__init__(label='Set Default Transcript Channel', style=discord.ButtonStyle.secondary, custom_id='admin_set_default_v1')

    async def callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done:
            await interaction.response.defer(ephemeral=True)
        modal = ChannelModal(title='Set default transcript channel', thread=None, action='set_default')
        return await interaction.followup.send(embed=make_embed('Modal opened', 'Check your client — a modal should open.'), ephemeral=True) or await interaction.response.send_modal(modal)

class AdminButton_PostToDefault(ui.Button):
    def __init__(self):
        super().__init__(label='Post Transcript to Default Channel', style=discord.ButtonStyle.primary, custom_id='admin_post_default_v1')

    async def callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done:
            await interaction.response.defer(ephemeral=True)
        thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
        if thread is None:
            return await interaction.followup.send(embed=make_embed('Invalid context', 'This command must be used inside a ticket thread.'), ephemeral=True)
        # get default from config
        default = get_config('transcript_channel_id')
        if not default:
            return await interaction.followup.send(embed=make_embed('Not configured', 'No default transcript channel configured.'), ephemeral=True)
        try:
            ch = interaction.guild.get_channel(int(default)) or await interaction.guild.fetch_channel(int(default))
            if not isinstance(ch, discord.TextChannel):
                raise Exception('Configured channel is not a text channel.')
            bio = await generate_transcript(thread)
            file = discord.File(fp=bio, filename=f"transcript-{thread.name}-{thread.id}.txt")
            await ch.send(content=f"📜 Transcript for ticket {thread.name} (id:{thread.id})", file=file)
            return await interaction.followup.send(embed=make_embed('Posted', f'Transcript posted to {ch.mention}'), ephemeral=True)
        except Exception as e:
            return await interaction.followup.send(embed=make_embed('Error', f'Failed to post transcript: {e}'), ephemeral=True)

class TicketThreadView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CloseButton())
        self.add_item(ClaimButton())
        self.add_item(TranscriptButton())
        self.add_item(LockButton())

class AdminPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(AdminButton_Delete())
        self.add_item(AdminButton_SendTranscript())
        self.add_item(AdminButton_PostToDefault())
        self.add_item(AdminButton_SetDefaultTranscript())

# --- Modals for admin actions ---

class ChannelModal(ui.Modal):
    """
    Modal asking for a channel mention or ID.
    action: 'send' -> send transcript to given channel
            'set_default' -> set config default transcript channel
    """
    channel_input = ui.TextInput(label='Channel mention or ID (e.g. #transcripts or 1234567890123)', style=discord.TextStyle.short, required=True, max_length=100)

    def __init__(self, title: str, thread: Optional[discord.Thread], action: str):
        super().__init__(title=title)
        self.thread = thread
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.channel_input.value.strip()
        # try mention like <#id> or plain id
        cid = None
        if raw.startswith('<#') and raw.endswith('>'):
            try:
                cid = int(raw[2:-1])
            except Exception:
                cid = None
        else:
            try:
                cid = int(raw)
            except Exception:
                cid = None
        if cid is None:
            # try by name fallback
            guild = interaction.guild
            if guild:
                ch = discord.utils.get(guild.text_channels, name=raw.lstrip('#'))
                if ch:
                    cid = ch.id
        if cid is None:
            return await interaction.response.send_message(embed=make_embed('Invalid channel', 'Could not parse channel ID or mention.'), ephemeral=True)

        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message(embed=make_embed('Error', 'Guild context missing.'), ephemeral=True)

        try:
            ch = guild.get_channel(cid) or await guild.fetch_channel(cid)
            if not isinstance(ch, discord.TextChannel):
                return await interaction.response.send_message(embed=make_embed('Invalid channel', 'The channel must be a text channel.'), ephemeral=True)
        except Exception as e:
            return await interaction.response.send_message(embed=make_embed('Error', f'Failed to fetch channel: {e}'), ephemeral=True)

        if self.action == 'set_default':
            set_config('transcript_channel_id', str(ch.id))
            return await interaction.response.send_message(embed=make_embed('Configured', f'Default transcript channel set to {ch.mention}'), ephemeral=True)

        if self.action == 'send':
            if not isinstance(self.thread, discord.Thread):
                return await interaction.response.send_message(embed=make_embed('Invalid context', 'This modal must be used from an admin panel inside a ticket thread.'), ephemeral=True)
            try:
                bio = await generate_transcript(self.thread)
                file = discord.File(fp=bio, filename=f"transcript-{self.thread.name}-{self.thread.id}.txt")
                await ch.send(content=f"📜 Transcript for ticket {self.thread.name} (id:{self.thread.id})", file=file)
                return await interaction.response.send_message(embed=make_embed('Sent', f'Transcript posted to {ch.mention}'), ephemeral=True)
            except Exception as e:
                return await interaction.response.send_message(embed=make_embed('Error', f'Failed to generate/send transcript: {e}'), ephemeral=True)

# --- Command handlers (helpers used by buttons and slash commands) ---

async def handle_claim(interaction: discord.Interaction):
    if not interaction.response.is_done:
        await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.followup.send(embed=make_embed('Invalid context', 'This must be used inside a ticket thread.'), ephemeral=True)

    cur = conn.cursor()
    cur.execute('SELECT * FROM tickets WHERE thread_id = ?', (str(channel.id),))
    row = cur.fetchone()
    if not row:
        return await interaction.followup.send(embed=make_embed('Not a ticket', 'This thread is not a known ticket.'), ephemeral=True)

    member = interaction.user
    if not is_staff(member):
        return await interaction.followup.send(embed=make_embed('Permission denied', 'Only staff can claim tickets.'), ephemeral=True)

    try:
        cur.execute('UPDATE tickets SET claimed_by = ? WHERE thread_id = ?', (str(member.id), str(channel.id)))
        conn.commit()
    except Exception:
        pass

    try:
        await channel.send(f'✅ Ticket claimed by {member.mention}')
    except Exception:
        pass

    return await interaction.followup.send(embed=make_embed('Ticket claimed', f'{member.mention} has claimed the ticket.'), ephemeral=True)

async def handle_close(interaction: discord.Interaction, reason: Optional[str]):
    # Defer early because we may do DB + transcript work
    if not interaction.response.is_done:
        await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.followup.send(embed=make_embed('Invalid context', 'This must be used inside a ticket thread.'), ephemeral=True)

    cur = conn.cursor()
    cur.execute('SELECT * FROM tickets WHERE thread_id = ?', (str(channel.id),))
    row = cur.fetchone()
    ticket_owner_id = int(row['user_id']) if row else None

    member = interaction.user
    if not (is_staff(member) or (ticket_owner_id == getattr(member, 'id', None))):
        return await interaction.followup.send(embed=make_embed('Permission denied', 'Only ticket creator or staff can close this ticket.'), ephemeral=True)

    try:
        await channel.edit(archived=True)
        cur.execute('UPDATE tickets SET status = ?, closed_at = ? WHERE thread_id = ?', ('closed', now_ts(), str(channel.id)))
        conn.commit()
    except Exception as e:
        return await interaction.followup.send(embed=make_embed('Error', f'Failed to archive thread: {e}'), ephemeral=True)

    # send transcript to default channel if configured
    default = get_config('transcript_channel_id')
    if default:
        try:
            ch = interaction.guild.get_channel(int(default)) or await interaction.guild.fetch_channel(int(default))
            if isinstance(ch, discord.TextChannel):
                bio = await generate_transcript(channel)
                file = discord.File(fp=bio, filename=f"transcript-{channel.name}-{channel.id}.txt")
                await ch.send(content=f"📜 Transcript for ticket {channel.name} (id:{channel.id})", file=file)
        except Exception:
            pass

    try:
        await channel.send(embed=make_embed('Ticket closed', 'This ticket has been closed and archived.'))
    except Exception:
        pass

    return await interaction.followup.send(embed=make_embed('Closed', 'Ticket closed.'), ephemeral=True)

async def handle_transcript(interaction: discord.Interaction):
    if not interaction.response.is_done:
        await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.followup.send(embed=make_embed('Invalid context', 'This must be used inside a ticket thread.'), ephemeral=True)

    cur = conn.cursor()
    cur.execute('SELECT * FROM tickets WHERE thread_id = ?', (str(channel.id),))
    row = cur.fetchone()
    if not row:
        return await interaction.followup.send(embed=make_embed('Not a ticket', 'This thread is not a known ticket.'), ephemeral=True)

    if not is_staff(interaction.user) and int(row['user_id']) != interaction.user.id:
        return await interaction.followup.send(embed=make_embed('Permission denied', 'Only ticket creator or staff can request transcripts.'), ephemeral=True)

    try:
        bio = await generate_transcript(channel)
        filename = f"transcript-{channel.name}-{channel.id}.txt"
        discord_file = discord.File(fp=bio, filename=filename)
        default = get_config('transcript_channel_id')
        posted = False
        if default:
            try:
                log_chan = interaction.guild.get_channel(int(default)) or await interaction.guild.fetch_channel(int(default))
                if isinstance(log_chan, discord.TextChannel):
                    await log_chan.send(content=f"📜 Transcript for ticket {channel.name} (id:{channel.id})", file=discord_file)
                    posted = True
            except Exception:
                posted = False
        if not posted:
            try:
                await interaction.user.send(embed=make_embed('Ticket transcript', f'Here is the transcript for {channel.name}'), file=discord_file)
                return await interaction.followup.send(embed=make_embed('Sent', 'Transcript has been sent via DM.'), ephemeral=True)
            except Exception:
                return await interaction.followup.send(embed=make_embed('Failed', 'Could not send transcript.'), ephemeral=True)
        else:
            return await interaction.followup.send(embed=make_embed('Posted', f'Transcript posted in <#{default}>.'), ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(embed=make_embed('Error', f'Failed to create transcript: {e}'), ephemeral=True)

async def handle_lock_toggle(interaction: discord.Interaction):
    if not interaction.response.is_done:
        await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.followup.send(embed=make_embed('Invalid context', 'This must be used inside a ticket thread.'), ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.followup.send(embed=make_embed('Permission denied', 'Only staff can lock/unlock tickets.'), ephemeral=True)
    try:
        new_locked = not getattr(channel, 'locked', False)
        await channel.edit(locked=new_locked)
        status = 'locked' if new_locked else 'unlocked'
        await channel.send(f'🔒 Ticket {status} by {interaction.user.mention}')
        return await interaction.followup.send(embed=make_embed('Done', f'Ticket {status}.'), ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(embed=make_embed('Error', f'Failed to toggle lock: {e}'), ephemeral=True)

# admin delete flow: confirmation view
class ConfirmDeleteView(ui.View):
    def __init__(self, thread: discord.Thread):
        super().__init__(timeout=60)
        self.thread = thread
        self.add_item(ui.Button(label='Confirm delete', style=discord.ButtonStyle.danger, custom_id=f'confirm_delete_{thread.id}'))
        self.add_item(ui.Button(label='Cancel', style=discord.ButtonStyle.secondary, custom_id=f'cancel_delete_{thread.id}'))

    @ui.button(label='Confirm delete', style=discord.ButtonStyle.danger, custom_id='confirm_delete_button')
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        # Only staff allowed
        if not is_staff(interaction.user):
            return await interaction.response.send_message(embed=make_embed('Permission denied', 'Only staff can delete threads.'), ephemeral=True)
        try:
            await self.thread.delete()
            cur = conn.cursor()
            cur.execute('UPDATE tickets SET status = ?, closed_at = ? WHERE thread_id = ?', ('deleted', now_ts(), str(self.thread.id)))
            conn.commit()
        except Exception as e:
            return await interaction.response.send_message(embed=make_embed('Error', f'Failed to delete thread: {e}'), ephemeral=True)
        return await interaction.response.send_message(embed=make_embed('Deleted', 'Thread has been deleted.'), ephemeral=True)

    @ui.button(label='Cancel', style=discord.ButtonStyle.secondary, custom_id='cancel_delete_button')
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        return await interaction.response.send_message(embed=make_embed('Cancelled', 'Delete cancelled.'), ephemeral=True)

async def admin_delete_flow(interaction: discord.Interaction):
    if not interaction.response.is_done:
        await interaction.response.defer(ephemeral=True)
    thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
    if thread is None:
        return await interaction.followup.send(embed=make_embed('Invalid context', 'This admin panel must be used inside a ticket thread.'), ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.followup.send(embed=make_embed('Permission denied', 'Only staff can delete threads.'), ephemeral=True)
    # send ephemeral confirmation with view
    view = ConfirmDeleteView(thread=thread)
    return await interaction.followup.send(embed=make_embed('Confirm delete', f'Are you sure you want to permanently delete thread **{thread.name}**?'), view=view, ephemeral=True)

# --- Slash Commands (English) ---

@bot.tree.command(name='ticket_setup', description='Post the ticket dropdown menu to a channel')
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(channel='Channel to post the ticket select menu into')
async def ticket_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild_id != GUILD_ID:
        return await interaction.response.send_message(embed=make_embed('Wrong Guild', 'This command is only allowed in the configured guild.'), ephemeral=True)
    bot_member = interaction.guild.me if interaction.guild else None
    perms = channel.permissions_for(bot_member) if bot_member else channel.permissions_for(interaction.guild.get_member(bot.user.id))
    if not (perms.send_messages and perms.create_private_threads and perms.read_message_history):
        return await interaction.response.send_message(embed=make_embed('Missing Permissions', 'I need Send Messages, Create Private Threads and Read Message History in that channel.'), ephemeral=True)
    embed = make_embed('Make a selection', 'Choose the appropriate option to open a ticket.')
    view = TicketSelectView()
    try:
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(embed=make_embed('Posted', f'Ticket menu posted in {channel.mention}'), ephemeral=True)
    except Exception as e:
        return await interaction.response.send_message(embed=make_embed('Error posting menu', f'Error:\n```\n{e}\n```'), ephemeral=True)

@bot.tree.command(name='ticket_close', description='Close the current ticket (thread).')
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def cmd_ticket_close(interaction: discord.Interaction, reason: Optional[str] = None):
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
    if not interaction.response.is_done:
        await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.followup.send(embed=make_embed('Invalid context', 'Only in ticket threads.'), ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.followup.send(embed=make_embed('Permission denied', 'Only staff can add members.'), ephemeral=True)
    try:
        await channel.add_user(member)
        return await interaction.followup.send(embed=make_embed('Added', f'{member.mention} added to the ticket.'), ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(embed=make_embed('Error', f'Error: {e}'), ephemeral=True)

@bot.tree.command(name='ticket_remove', description='Remove a member from the ticket thread (staff only).')
@app_commands.guilds(discord.Object(id=GUILD_ID))
@app_commands.describe(member='Member to remove from the ticket thread')
async def cmd_ticket_remove(interaction: discord.Interaction, member: discord.Member):
    if not interaction.response.is_done:
        await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        return await interaction.followup.send(embed=make_embed('Invalid context', 'Only in ticket threads.'), ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.followup.send(embed=make_embed('Permission denied', 'Only staff can remove members.'), ephemeral=True)
    try:
        await channel.remove_user(member)
        return await interaction.followup.send(embed=make_embed('Removed', f'{member.mention} removed from the ticket.'), ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(embed=make_embed('Error', f'Error: {e}'), ephemeral=True)

@bot.tree.command(name='ticket_lock', description='Lock or unlock the ticket (staff only).')
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def cmd_ticket_lock(interaction: discord.Interaction):
    await handle_lock_toggle(interaction)

@bot.tree.command(name='admin_panel', description='Open the admin panel for the current ticket (staff only). Use inside a ticket thread.')
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def cmd_admin_panel(interaction: discord.Interaction):
    if not interaction.response.is_done:
        await interaction.response.defer(ephemeral=True)
    thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
    if thread is None:
        return await interaction.followup.send(embed=make_embed('Invalid context', 'You must run this command inside a ticket thread.'), ephemeral=True)
    if not is_staff(interaction.user):
        return await interaction.followup.send(embed=make_embed('Permission denied', 'Only staff can open the admin panel.'), ephemeral=True)
    view = AdminPanelView()
    return await interaction.followup.send(embed=make_embed('Admin Panel', f'Admin controls for {thread.name}'), view=view, ephemeral=True)

# --- on_ready: sync commands & register persistent views ---

@bot.event
async def on_ready():
    print('Logged in as', bot.user, '— id:', bot.user.id)
    # register persistent views so buttons work after restart
    try:
        bot.add_view(TicketSelectView())
        persistent_thread_view = ui.View(timeout=None)
        persistent_thread_view.add_item(CloseButton())
        persistent_thread_view.add_item(ClaimButton())
        persistent_thread_view.add_item(TranscriptButton())
        persistent_thread_view.add_item(LockButton())
        bot.add_view(persistent_thread_view)
        # admin panel persistent view (buttons do not rely on per-thread state)
        bot.add_view(AdminPanelView())
    except Exception as e:
        print('Could not add persistent views:', e)

    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
        print('Command tree synced to guild', GUILD_ID)
    except Exception as e:
        print('Command sync failed for guild, attempting global sync fallback:', e)
        try:
            await bot.tree.sync()
            print('Global sync succeeded')
        except Exception as ge:
            print('Global sync failed as well:', ge)
    try:
        print('Registered app commands:', bot.tree.commands)
    except Exception:
        pass

    # auto-post menu if configured
    if POST_CHANNEL_ID:
        try:
            ch = bot.get_channel(POST_CHANNEL_ID) or await bot.fetch_channel(POST_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                await ch.send(embed=make_embed('Make a selection', 'Choose the appropriate option to open a ticket.'), view=TicketSelectView())
                print('Posted ticket menu automatically in', ch.id)
        except Exception as e:
            print('Could not auto-post menu:', e)

# --- Run ---
if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
