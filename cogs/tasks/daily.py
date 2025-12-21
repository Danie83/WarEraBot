import aiohttp
from discord.ext import commands, tasks
from utils.api import get_user
from utils.computational import triangular
from config import config

ECONOMY_SKILLS = ['energy', 'companies', 'entrepreneurship', 'production']

class DailyTasks(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cached_members = {}
        self.daily_job.start()

    def cog_unload(self):
        self.daily_job.cancel()

    @tasks.loop(hours=24)
    async def daily_job(self):
        guild = self.bot.get_guild(config['guild'])
        citizen = guild.get_role(config['roles']['citizen'])
        economy_role = guild.get_role(config['roles']['economy'])
        fight_role = guild.get_role(config['roles']['fight'])
        
        members = citizen.members
        async with aiohttp.ClientSession() as session:
            for member in members:
                user = await get_user(member.display_name, session)
                if user is None:
                    continue
                economy_skill_points = 0
                fight_skill_points = 0
                for skill_name, skill_data in user['skills'].items():
                    level = skill_data['level']
                    if level != 0:
                        if skill_name in ECONOMY_SKILLS:
                            economy_skill_points += triangular(level)
                        else:
                            fight_skill_points += triangular(level)
                total_skill_points = user['leveling']['totalSkillPoints']
                unspent_skill_points = user['leveling']['availableSkillPoints']

                # division by zero, should not be possible (level 1 = 4 points already)
                if total_skill_points == 0:
                    continue

                percentage = ((economy_skill_points + unspent_skill_points) / total_skill_points) * 100
                is_economy = percentage > 50
                previous = self.cached_members.get(member.id)
                if previous is not None and previous == is_economy:
                    continue

                if is_economy:
                    if economy_role not in member.roles:
                        await member.add_roles(economy_role, reason="Economy skill > 50")
                    if fight_role in member.roles:
                        await member.remove_roles(fight_role, reason="Economy > 50, remove fighter role")
                else:
                    if fight_role not in member.roles:
                        await member.add_roles(fight_role, reason="Economy skill <= 50")
                    if economy_role in member.roles:
                        await member.remove_roles(economy_role, reason="Economy <= 50, remove economy role")
                
                self.cached_members[member.id] = is_economy

    @daily_job.before_loop
    async def before_daily_job(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(DailyTasks(bot))