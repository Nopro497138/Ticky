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

GUILD_ID = int(os.getenv('GUILD_ID')) if os.getenv('GUILD_ID') else None
STAFF_ROLE_ID = int(os.getenv('STAFF_ROLE_ID')) if os.getenv('STAFF_ROLE_ID') else None
DB_PATH = os.getenv('DB_PATH', './tickets.sqlite')
POST_CHANNEL_ID = int(os.getenv('POST_CHANNEL_ID')) if os.getenv('POST_CHANNEL_ID') else None
STAFF_ADD_LIMIT = int(os.getenv('STAFF_ADD_LIMIT', '20'))

if not TOKEN or not GUILD_ID:
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
        super().__init__(placeholder='Wähle einen Grund für dein Ticket', min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        choice = self.values[0]
        user = interaction.user
        channel = interaction.channel

        if not channel or not isinstance(channel, discord.TextChannel):
            return await interaction.followup.send('Fehler: Kanal nicht gefunden.', ephemeral=True)

        perms = channel.permissions_for(interaction.guild.me)
        if not perms.create_private_threads:
            return await interaction.followup.send('Ich habe keine Berechtigung, private Threads zu erstellen.', ephemeral=True)

        thread_name = thread_safe_name(choice, user.name)
        try:
            thread = await channel.create_thread(name=thread_name, type=discord.ChannelType.private_thread, auto_archive_duration=1440)
        except Exception as e:
            return await interaction.followup.send(f'Fehler beim Erstellen des Threads: {e}', ephemeral=True)

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
                else:
                    # no members with role
                    pass

        # save ticket
        try:
            cur = conn.cursor()
            cur.execute('INSERT OR IGNORE INTO tickets (thread_id, user_id, choice, created_at, status) VALUES (?, ?, ?, ?, ?)',
                        (str(thread.id), str(user.id), choice, now_ts(), 'open'))
            conn.commit()
        except Exception:
            pass

        # send welcome message with close button
        v = TicketThreadView(thread_owner_id=user.id)
        human = {'purchase': 'Purchase Items', 'staff': 'Staff Help', 'other': 'Other'}.get(choice, choice)
        content = f'Hallo {user.mention}, danke für dein Ticket ({human}). Ein Mitglied des Staffs wird gleich antworten.'
        if fallback_role_mention and STAFF_ROLE_ID:
            content += f"

Hinweis: Viele Staff-Mitglieder vorhanden — ping: <@&{STAFF_ROLE_ID}>"

        try:
            await thread.send(content=content, view=v)
        except Exception:
            # ignore send errors
            pass

        return await interaction.followup.send(f'Ticket erstellt: {thread.mention}', ephemeral=True)


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
            return await interaction.response.send_message('Dieser Knopf funktioniert nur in Ticket-Threads.', ephemeral=True)

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
            return await interaction.response.send_message('Nur der Ticket-Ersteller oder Staff kann dieses Ticket schließen.', ephemeral=True)

        try:
            await channel.edit(archived=True)
            cur.execute('UPDATE tickets SET status = ?, closed_at = ? WHERE thread_id = ?', ('closed', now_ts(), str(channel.id)))
            conn.commit()
            return await interaction.response.send_message('Ticket wurde geschlossen und archiviert.', ephemeral=True)
        except Exception as e:
            return await interaction.response.send_message(f'Fehler beim Schließen des Tickets: {e}', ephemeral=True)


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
        return await interaction.response.send_message('Dieser Befehl ist nur in der konfigurierten Gilde erlaubt.', ephemeral=True)

    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.send_messages and perms.create_private_threads and perms.read_message_history):
        return await interaction.response.send_message('Bot benötigt: Send Messages, Create Private Threads, Read Message History in diesem Kanal.', ephemeral=True)

    embed = discord.Embed(title='Make a selection', description='Wähle die passende Option aus, um ein Ticket zu eröffnen.', color=0x5865F2)
    view = TicketSelectView()
    try:
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(f'Ticket-Auswahl wurde in {channel.mention} gepostet.', ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f'Fehler beim Posten: {e}', ephemeral=True)


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
                embed = discord.Embed(title='Make a selection', description='Wähle die passende Option aus, um ein Ticket zu eröffnen.', color=0x5865F2)
                await ch.send(embed=embed, view=TicketSelectView())
                print('Posted ticket menu automatically in', ch.id)
        except Exception as e:
            print('Could not auto-post menu:', e)



bot.run(os.getenv("DISCORD_TOKEN"))
