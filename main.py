import os
import sqlite3
import asyncio
from datetime import datetime
from typing import Optional

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
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()
c.execute('''
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT UNIQUE,
    user_id TEXT,
    choice TEXT,
    created_at INTEGER,
    closed_at INTEGER,
    status TEXT
)
''')
conn.commit()

# --- Helpers ---

def now_ts() -> int:
    return int(datetime.utcnow().timestamp())

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

# --- UI Components ---

class TicketSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label='Purchase Items', value='purchase', description='Buy any item in our market!', emoji='💰'),
            discord.SelectOption(label='Staff Help', value='staff', description='Reach staff about your questions and concerns!', emoji='⚙️'),
            discord.SelectOption(label='Other', value='other', description='All other questions or requests', emoji='❓')
        ]
        super().__init__(placeholder='Choose a reason for your ticket', min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        choice = self.values[0]
        user = interaction.user
        channel = interaction.channel

        if not channel or not isinstance(channel, discord.TextChannel):
            embed = make_embed('Error', 'Channel not found.')
            return await interaction.followup.send(embed=embed, ephemeral=True)

        perms = channel.permissions_for(interaction.guild.me)
        if not perms.create_private_threads:
            embed = make_embed('Permission error', 'I do not have permission to create private threads in this channel.')
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
            # not critical; continue
            pass

        # try to add staff members up to the limit
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
                            await asyncio.sleep(0.25)  # small pause to reduce rate pressure
                        except Exception:
                            continue
                    if len(members) > STAFF_ADD_LIMIT:
                        fallback_role_mention = True

        # save ticket
        try:
            cur = conn.cursor()
            cur.execute('INSERT OR IGNORE INTO tickets (thread_id, user_id, choice, created_at, status) VALUES (?, ?, ?, ?, ?)',
                        (str(thread.id), str(user.id), choice, now_ts(), 'open'))
            conn.commit()
        except Exception:
            pass

        # send welcome message with close button (embed)
        v = TicketThreadView(thread_owner_id=user.id)
        human = {'purchase': 'Purchase Items', 'staff': 'Staff Help', 'other': 'Other'}.get(choice, choice)
        description = f'Hello {user.mention}, thanks for your ticket ({human}). A staff member will respond shortly.'
        if fallback_role_mention and STAFF_ROLE_ID:
            description += f'\n\nNote: Many staff members detected — pinging role: <@&{STAFF_ROLE_ID}>'

        thread_embed = make_embed('New Ticket', description)
        try:
            await thread.send(embed=thread_embed, view=v)
        except Exception:
            # ignore send errors
            pass

        # ephemeral confirmation embed to the user
        confirm_embed = make_embed('Ticket Created', f'Your ticket has been created: {thread.mention}')
        return await interaction.followup.send(embed=confirm_embed, ephemeral=True)


class TicketSelectView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


class CloseButton(ui.Button):
    def __init__(self, thread_owner_id: Optional[int]):
        super().__init__(label='Close ticket', style=discord.ButtonStyle.danger)
        self.thread_owner_id = thread_owner_id

    async def callback(self, interaction: discord.Interaction):
        # must be used in a thread context
        channel = interaction.channel
        if not channel or not isinstance(channel, discord.Thread):
            embed = make_embed('Invalid Context', 'This button only works inside ticket threads.')
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        # fetch ticket info from DB
        cur = conn.cursor()
        cur.execute('SELECT * FROM tickets WHERE thread_id = ?', (str(channel.id),))
        row = cur.fetchone()
        ticket_owner_id = int(row['user_id']) if row else None

        member = interaction.user
        is_staff = False
        if STAFF_ROLE_ID:
            role = interaction.guild.get_role(STAFF_ROLE_ID)
            if role and isinstance(interaction.user, discord.Member):
                is_staff = role in interaction.user.roles

        is_owner = ticket_owner_id == member.id
        if not (is_staff or is_owner):
            embed = make_embed('Permission Denied', 'Only the ticket creator or staff can close this ticket.')
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        try:
            await channel.edit(archived=True)
            cur.execute('UPDATE tickets SET status = ?, closed_at = ? WHERE thread_id = ?', ('closed', now_ts(), str(channel.id)))
            conn.commit()
            embed = make_embed('Ticket Closed', 'Ticket closed and archived.')
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            embed = make_embed('Error Closing Ticket', f'An error occurred while closing the ticket:\n```\n{e}\n```')
            return await interaction.response.send_message(embed=embed, ephemeral=True)


class TicketThreadView(ui.View):
    def __init__(self, thread_owner_id: Optional[int]):
        super().__init__(timeout=None)
        self.add_item(CloseButton(thread_owner_id))


# --- Slash command to post the menu ---

@bot.tree.command(name='ticket_setup', description='Post the ticket dropdown menu to a channel')
@app_commands.describe(channel='Channel to post the ticket select menu into')
async def ticket_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    # only allow in configured guild
    if interaction.guild_id != GUILD_ID:
        embed = make_embed('Wrong Guild', 'This command is only allowed in the configured guild.')
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.create_private_threads and perms.read_message_history):
        embed = make_embed('Missing Permissions', 'Bot requires: Send Messages, Create Private Threads, Read Message History in that channel.')
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    embed = make_embed('Make a selection', 'Choose the appropriate option to open a ticket.')
    view = TicketSelectView()
    try:
        await channel.send(embed=embed, view=view)
        confirm = make_embed('Posted', f'Ticket menu posted in {channel.mention}.')
        await interaction.response.send_message(embed=confirm, ephemeral=True)
    except Exception as e:
        error_embed = make_embed('Error posting menu', f'An error occurred while posting the menu:\n```\n{e}\n```')
        await interaction.response.send_message(embed=error_embed, ephemeral=True)


# --- on_ready: sync commands & optionally auto-post menu ---

@bot.event
async def on_ready():
    print('Logged in as', bot.user)
    # sync tree to the specific guild for faster iteration
    try:
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    except Exception as e:
        print('Command sync failed:', e)

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


if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
