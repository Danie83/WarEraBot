import discord
from discord.ext import commands
from config import config

intents = discord.Intents.default()
intents.members = True

class WarEraBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        await self.load_extension("cogs.tasks.jobs")
        await self.tree.sync()

bot = WarEraBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

bot.run(config['token'])