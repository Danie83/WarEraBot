from discord.ext import commands, tasks
from utils.api import get_user
from config import config

class DailyTasks(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cached_members = {}
        self.daily_job.start()

    def cog_unload(self):
        self.daily_job.cancel()

    @tasks.loop(seconds=10)
    async def daily_job(self):
        guild = self.bot.get_guild(config['guild'])
        citizen = guild.get_role(config['roles']['citizen'])
        economy_role = guild.get_role(config['roles']['economy'])
        fight_role = guild.get_role(config['roles']['fight'])
        
        members = citizen.members
        for member in members:
            user = get_user(member.display_name)
            if user is None:
                continue
            economy_skills = ['energy', 'companies', 'entrepreneurship', 'production']
            economy_skill_points = 0
            fight_skill_points = 0
            for skill_name, skill_data in user['skills'].items():
                level = skill_data['level']
                if level != 0:
                    if skill_name in economy_skills:
                        economy_skill_points += (level * (level + 1)) / 2
                    else:
                        fight_skill_points += (level * (level + 1)) / 2
            total_skill_points = user['leveling']['totalSkillPoints']
            unspent_skill_points = user['leveling']['availableSkillPoints']

            percentage = ((economy_skill_points + unspent_skill_points) / total_skill_points) * 100
            if percentage > 50:
                if economy_role not in member.roles:
                    await member.add_roles(economy_role, reason="Economy skill > 50")
                if fight_role in member.roles:
                    await member.remove_roles(fight_role, reason="Economy > 50, remove fighter role")
            else:
                if fight_role not in member.roles:
                    await member.add_roles(fight_role, reason="Economy skill <= 50")
                if economy_role in member.roles:
                    await member.remove_roles(economy_role, reason="Economy <= 50, remove economy role")

    @daily_job.before_loop
    async def before_daily_job(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(DailyTasks(bot))