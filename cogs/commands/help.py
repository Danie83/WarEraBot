import discord
from discord import app_commands
from discord.ext import commands
from typing import List, Optional


class Help(commands.Cog):
    """Provides a paginated `/help` command with two pages:
    - Commands: lists available bot commands and usage
    - Jobs: lists background tasks (jobs) and how often they run
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Show bot commands and background jobs (paginated).")
    async def help(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Page 1: Commands
        commands_embed = discord.Embed(title="Bot Commands", color=discord.Color.blurple())
        commands_embed.description = "Use the slash commands below. Autocomplete is available where applicable."

        commands_embed.add_field(
            name="/diplomacy [country_name]",
            value=(
                "Show diplomacy info for countries. If `country_name` is provided, shows details for that country; "
                "otherwise shows a paginated list of countries with diplomacy records (3 per page). Government-only fields "
                "(diplomacy list) are shown when you have the government role."
            ),
            inline=False,
        )

        commands_embed.add_field(
            name="/update_diplomacy country_name [status] [diplomacy] [description]",
            value=(
                "Government-only. Update an existing diplomacy record: set `status`, append a `diplomacy` entry, or update `description`. "
                "Status must be one of the predefined options and autocomplete is available."
            ),
            inline=False,
        )

        commands_embed.add_field(
            name="/add_diplomacy country_name [status] [description]",
            value=(
                "Government-only. Create a new diplomacy record for a country. If a record already exists, use `/update_diplomacy`."
            ),
            inline=False,
        )

        commands_embed.add_field(
            name="/remove_diplomacy country_name position",
            value=("Government-only. Remove the diplomacy list entry at the provided 1-based `position`."),
            inline=False,
        )

        commands_embed.add_field(
            name="/delete_diplomacy country_name",
            value=("Government-only. Delete the diplomacy record for the specified country."),
            inline=False,
        )

        commands_embed.add_field(
            name="/fightstatus [military_unit]",
            value=(
                "Fetch fight status for fighters. Without `military_unit`, the command operates on members with the configured 'fight' role; "
                "with `military_unit` it fetches members of that unit. Results are paginated (10 per page) and include buff/debuff status, "
                "health/hunger, level, and online state. Filters for Buffed/Neutral/Debuffed are available in the paginator."
            ),
            inline=False,
        )

        commands_embed.set_footer(text="Commands page — use the buttons to view Jobs or close this help.")

        # Page 2: Jobs (background tasks)
        jobs_embed = discord.Embed(title="Background Jobs / Tasks", color=discord.Color.dark_gold())
        jobs_embed.description = "Short summary of active background tasks and how often they run."

        jobs_embed.add_field(
            name="skill_roles",
            value=(
                "Runs every 1 hour. Scans server members with the Citizen role and assigns/removes Economy or Fighter roles based on their in-game skill distribution. "
                "Sends a summary to the reports channel when changes occur."
            ),
            inline=False,
        )

        jobs_embed.add_field(
            name="military_unit_roles",
            value=(
                "Runs every 3 hours. Assigns Military Unit roles to members based on their in-game MU membership and removes conflicting MU roles. "
                "Sends a summary to the reports channel when changes occur."
            ),
            inline=False,
        )

        jobs_embed.add_field(
            name="unidentified_members",
            value=(
                "Runs every 6 hours. Checks Citizen/Newbie members to see if their nickname maps to a known game user. "
                "Records mappings when found and reports unidentified players to the reports channel."
            ),
            inline=False,
        )

        jobs_embed.add_field(
            name="takeover_countries",
            value=(
                "Runs every 5 minutes. Scans countries and reports those that appear empty (no government/congress members), "
                "posting takeover opportunities to the public channel."
            ),
            inline=False,
        )

        jobs_embed.add_field(
            name="buff_monitor",
            value=(
                "Runs every 10 minutes. Monitors fighter buffs and notifies users when their active pill buff is nearing expiration (uses an internal cache to avoid repeated notifications)."
            ),
            inline=False,
        )

        jobs_embed.add_field(
            name="bounty_monitor",
            value=(
                "Runs on the configured interval (`BOUNTY_MONITOR_INTERVAL_MINUTES`). Checks active battles for money pools/bounties and posts a summary to the public channel when relevant."
            ),
            inline=False,
        )

        jobs_embed.set_footer(text="Jobs page — intervals shown as implemented in the Jobs cog (see cogs/tasks/jobs.py).")

        embeds: List[discord.Embed] = [commands_embed, jobs_embed]

        class Paginator(discord.ui.View):
            def __init__(self, embeds: List[discord.Embed], author: discord.User, timeout: float = 120.0):
                super().__init__(timeout=timeout)
                self.embeds = embeds
                self.author = author
                self.index = 0

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                return True

            @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
            async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.index = (self.index - 1) % len(self.embeds)
                await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

            @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
            async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.index = (self.index + 1) % len(self.embeds)
                await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

            @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
            async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
                try:
                    self.stop()
                except Exception:
                    pass
                await interaction.response.edit_message(view=None)

        paginator = Paginator(embeds, interaction.user)
        try:
            await interaction.followup.send(embed=embeds[0], view=paginator)
        except Exception:
            channel = getattr(interaction, 'channel', None)
            if channel:
                await channel.send(embed=embeds[0], view=paginator)
            else:
                await interaction.followup.send("Unable to display help at this time.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
