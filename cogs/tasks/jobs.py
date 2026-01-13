import aiohttp
import discord
from discord.ext import commands, tasks
from utils.api import get_user, get_all_countries, get_country_government
from utils.computational import triangular
from config import config

ECONOMY_SKILLS = ['energy', 'companies', 'entrepreneurship', 'production']

class Jobs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cached_members = {}
        self.countries = None
        self.skill_roles.start()
        self.military_unit_roles.start()
        self.unidentified_members.start()
        self.takeover_countries.start()

    def cog_unload(self):
        self.skill_roles.cancel()
        self.military_unit_roles.cancel()
        self.unidentified_members.cancel()
        self.takeover_countries.cancel()
    
    async def get_countries(self):
        async with aiohttp.ClientSession() as session:
            return await get_all_countries(session)

    @tasks.loop(hours=24)
    async def skill_roles(self):
        """Parses all members of the server that hold the citizen role and assigns
           roles based on their assigned skills (economy or fighter)
        """
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

    @skill_roles.before_loop
    async def before_skill_roles(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def military_unit_roles(self):
        """Parses all members of the server that hold the citizen role and assigns
           military unit roles based on the available MU server roles available.
        """
        guild = self.bot.get_guild(config['guild'])
        citizen = guild.get_role(config['roles']['citizen'])
        military_units = config['military_units']
        mu_to_role = {unit['id'] : guild.get_role(unit['roleId']) for unit in military_units}

        members = citizen.members
        async with aiohttp.ClientSession() as session:
            for member in members:
                user = await get_user(member.display_name, session)
                if user is None or "mu" not in user.keys():
                    continue
                role = mu_to_role.get(user["mu"])
                if role is None:
                    continue
                if role in member.roles:
                    continue
                roles_to_remove = [
                    r for r in mu_to_role.values()
                    if r and r in member.roles and r != role
                ]
                await member.add_roles(role, reason="Assigned Military Unit role.")
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason="Removed unused Military Unit roles.")

    @military_unit_roles.before_loop
    async def before_military_unit_roles(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def unidentified_members(self):
        """Parses all members of the server that hold the citizen role and checks
           if their server nickname matches the one from the game.
        """
        guild = self.bot.get_guild(config['guild'])
        citizen = guild.get_role(config['roles']['citizen'])
        unidentified = []
        async with aiohttp.ClientSession() as session:
            for member in citizen.members:
                user = await get_user(member.display_name, session)
                if user is None:
                    unidentified.append(member)
                    continue
            if len(unidentified) == 0:
                return
            channel = guild.get_channel(config["channels"]["reports"])
            embed = self.build_unidentified_embed(unidentified)
            await channel.send(embed=embed)

    @unidentified_members.before_loop
    async def before_unidentified_members(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def takeover_countries(self):
        """Parses all countries of the server and posts any country that can be taken over.
        """

        if self.countries is None:
            self.countries = await self.get_countries()

        if self.countries is None or len(self.countries) == 0:
            return
        
        guild = self.bot.get_guild(config['guild'])
        active_countries = config['active_countries']
        async with aiohttp.ClientSession() as session:
            empty_countries = []
            for country in self.countries:
                if active_countries is not None and len(active_countries) != 0:
                    if country['name'] in active_countries:
                        continue
                government = await get_country_government(country['_id'], session)
                # country is empty, api displays only _id, country, __v, and congressMembers keys .
                if len(government.keys()) == 4 and len(government['congressMembers']) == 0:
                    empty_countries.append((country['name'], country['_id']))
            if len(empty_countries) == 0:
                return
            channel = guild.get_channel(config["channels"]["reports"])
            embed = self.build_takeover_embed(empty_countries)
            await channel.send(embed=embed)

    @takeover_countries.before_loop
    async def before_takeover_countries(self):
        await self.bot.wait_until_ready()

    def build_takeover_embed(self, countries) -> discord.Embed:
        embed = discord.Embed(
            title="Takeover Countries Found",
            description="The following countries can be captured:",
            color=discord.Color.orange()
        )
        lines = [f"* {c[0]} ('https://app.warera.io/country/{c[1]}')" for c in countries]
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 1000:
                embed.add_field(name="Countries", value=chunk, inline=False)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            embed.add_field(name="Countries", value=chunk, inline=False)
        embed.set_footer(text=f"Total: {len(countries)}")
        return embed
        
    def build_unidentified_embed(self, members: list[discord.Member]) -> discord.Embed:
        embed = discord.Embed(
            title="Unidentified Players Found",
            description="The following members could not be matched:",
            color=discord.Color.orange()
        )
        lines = [f"* {m.display_name} ('{m.id}')" for m in members]
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 1000:
                embed.add_field(name="Players", value=chunk, inline=False)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            embed.add_field(name="Players", value=chunk, inline=False)
        embed.set_footer(text=f"Total: {len(members)}")
        return embed
    
async def setup(bot: commands.Bot):
    await bot.add_cog(Jobs(bot))