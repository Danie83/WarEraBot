import discord
import asyncio
from discord.ext import commands, tasks
from utils.api import get_user, get_all_countries, get_country_government, get_user_info, get_shared_session, get_active_battles, get_country, get_military_unit, get_mercenary_auctions
from utils.db import init_db, save_user, find_api_id_by_display_name, find_api_id_by_discord_username, get_record_by_api_id
from utils.computational import triangular
from config import config

ECONOMY_SKILLS = ['energy', 'companies', 'entrepreneurship', 'production']
from datetime import datetime, timezone, timedelta

# minutes before buff end to notify the user
NOTIFY_THRESHOLD_MINUTES = 30
# how long to wait before re-checking users with no active buff/debuff
DEFAULT_SKIP_HOURS = 1
# how often the buff monitor runs (minutes) — keep in sync with @tasks.loop(minutes=...)
BUFF_MONITOR_INTERVAL_MINUTES = 10
# how often the bounty monitor runs (minutes)
BOUNTY_MONITOR_INTERVAL_MINUTES = 1
# how often the mercenary auction monitor runs (minutes)
MERCENARY_MONITOR_INTERVAL_MINUTES = 1
# effective notify threshold to account for the monitor interval so users are
# guaranteed to be notified at least NOTIFY_THRESHOLD_MINUTES before expiry
EFFECTIVE_NOTIFY_MINUTES = NOTIFY_THRESHOLD_MINUTES

class Jobs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cached_members = {}
        # track displayed bounties: key -> last seen bountyEffectiveAt
        # key format: "{battle_id}:{side}" where side is 'attacker' or 'defender'
        self.displayed_bounties: dict = {}
        # track displayed mercenary auctions: auction_id -> token (updatedAt/currentPayout)
        self.displayed_auctions: dict = {}
        # cache for buff checks: api_id -> { next_check: datetime, notified_for_end_at: str|None }
        self.buff_check_cache: dict = {}
        self.countries = None
        # Ensure database/table exists
        try:
            init_db()
        except Exception:
            pass
        self.skill_roles.start()
        self.military_unit_roles.start()
        self.commander_roles.start()
        self.unidentified_members.start()
        self.takeover_countries.start()
        self.buff_monitor.start()
        self.bounty_monitor.start()
        self.mercenary_monitor.start()

    def cog_unload(self):
        self.skill_roles.cancel()
        self.military_unit_roles.cancel()
        self.commander_roles.cancel()
        self.unidentified_members.cancel()
        self.takeover_countries.cancel()
        self.buff_monitor.cancel()
        self.bounty_monitor.cancel()
        self.mercenary_monitor.cancel()

    async def get_countries(self):
        session = await get_shared_session()
        return await get_all_countries(session)

    @tasks.loop(hours=1)
    async def skill_roles(self):
        """Parses all members of the server that hold the citizen role and assigns
           roles based on their assigned skills (economy or fighter)
        """
        guild = self.bot.get_guild(config['guild'])
        if guild is None:
            return
        citizen = guild.get_role(config['roles']['citizen'])
        economy_role = guild.get_role(config['roles']['economy'])
        fight_role = guild.get_role(config['roles']['fight'])
        
        members = citizen.members if citizen else []
        stats = {
            'economy_added': [],
            'economy_removed': [],
            'fight_added': [],
            'fight_removed': [],
        }
        session = await get_shared_session()
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
                    if economy_role and economy_role not in member.roles:
                        await member.add_roles(economy_role, reason="Economy skill > 50")
                        stats['economy_added'].append(member.display_name)
                    if fight_role and fight_role in member.roles:
                        await member.remove_roles(fight_role, reason="Economy > 50, remove fighter role")
                        stats['fight_removed'].append(member.display_name)
                else:
                    if fight_role and fight_role not in member.roles:
                        await member.add_roles(fight_role, reason="Economy skill <= 50")
                        stats['fight_added'].append(member.display_name)
                    if economy_role and economy_role in member.roles:
                        await member.remove_roles(economy_role, reason="Economy <= 50, remove economy role")
                        stats['economy_removed'].append(member.display_name)
                
                self.cached_members[member.id] = is_economy

        # Send a summary embed for the run only if there were changes
        channel = guild.get_channel(config["channels"]["reports"]) if guild else None
        if channel:
            total_changes = sum(len(stats.get(k, [])) for k in ('economy_added', 'economy_removed', 'fight_added', 'fight_removed'))
            if total_changes > 0:
                embed = self.build_skill_roles_embed(stats)
                if embed:
                    await channel.send(embed=embed)

    @skill_roles.before_loop
    async def before_skill_roles(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=3)
    async def military_unit_roles(self):
        """Parses all members of the server that hold the citizen role and assigns
           military unit roles based on the available MU server roles available.
        """
        guild = self.bot.get_guild(config['guild'])
        if guild is None:
            return
        citizen = guild.get_role(config['roles']['citizen'])
        newbie = guild.get_role(config['roles']['newbie'])
        military_units = config.get('military_units', [])
        mu_to_role = {unit['id'] : guild.get_role(unit['roleId']) for unit in military_units}

        # Build a mapping of manager_api_id -> set of MU ids they manage.
        # We need an active session to call the API.
        session = await get_shared_session()
        owners: dict = {}
        for unit in military_units:
            try:
                mu_data = await get_military_unit(unit['id'], session)
            except Exception:
                mu_data = None
            if not mu_data:
                continue
            # The API returns role.managers as an array of api user ids
            managers = []
            try:
                managers = (mu_data.get('roles') or {}).get('managers') or []
            except Exception:
                managers = []
            for mgr in managers:
                # map manager api id to a set of unit ids (support multiple)
                if mgr in owners:
                    owners[mgr].add(unit['id'])
                else:
                    owners[mgr] = {unit['id']}

        members = set()
        if citizen:
            members.update(citizen.members)
        if newbie:
            members.update(newbie.members)

        # track player display names added/removed per role
        added_members: dict = {}
        removed_members: dict = {}

        # For each member, determine the desired MU-related roles (their current MU
        # plus any MU(s) they own/manage) then add/remove server roles to match.
        for member in members:
            try:
                user = await get_user(member.display_name, session)
            except Exception:
                user = None
            if user is None:
                continue

            api_id = user.get('_id') if isinstance(user, dict) else None

            # Desired roles set (discord.Role objects)
            desired_roles = set()

            # 1) Current MU role (if the user belongs to one)
            mu_id = user.get('mu')
            if mu_id:
                r = mu_to_role.get(mu_id)
                if r:
                    desired_roles.add(r)

            # 2) Owner/manager MU roles (if the user's api id is a manager)
            if api_id and api_id in owners:
                for owned_mu in owners.get(api_id, set()):
                    r = mu_to_role.get(owned_mu)
                    if r:
                        desired_roles.add(r)

            # Roles currently on the member that are MU roles we manage
            current_mu_roles = {r for r in mu_to_role.values() if r and r in member.roles}

            # Add roles that are desired but missing
            to_add = [r for r in desired_roles if r not in member.roles]
            if to_add:
                try:
                    await member.add_roles(*to_add, reason="Assigned Military Unit role.")
                    for r in to_add:
                        name = r.name if r else str(getattr(r, 'id', 'unknown'))
                        added_members.setdefault(name, []).append(member.display_name)
                except Exception:
                    pass

            # Remove MU roles that the member should no longer have (managed set minus desired)
            roles_to_remove = [r for r in current_mu_roles if r not in desired_roles]
            if roles_to_remove:
                try:
                    await member.remove_roles(*roles_to_remove, reason="Removed unused Military Unit roles.")
                    for r in roles_to_remove:
                        rname = r.name if r else str(getattr(r, 'id', 'unknown'))
                        removed_members.setdefault(rname, []).append(member.display_name)
                except Exception:
                    pass

        # Send a summary embed for military unit role changes — only if there were changes
        channel = guild.get_channel(config["channels"]["reports"]) if guild else None
        if channel:
            total_changes = sum(len(v) for v in added_members.values()) + sum(len(v) for v in removed_members.values())
            if total_changes == 0:
                return
            embed = self.build_military_unit_embed(added_members, removed_members)
            if embed:
                await channel.send(embed=embed)

    @military_unit_roles.before_loop
    async def before_military_unit_roles(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=3)
    async def commander_roles(self):
        """Syncs the Discord commander role with commanders configured in all military units."""
        guild = self.bot.get_guild(config['guild'])
        if guild is None:
            return

        commander_role = guild.get_role(config['roles']['commander'])
        if commander_role is None:
            return

        session = await get_shared_session()
        military_units = config.get('military_units', [])
        commander_ids = set()

        # Gather commander api ids from all military units
        for unit in military_units:
            try:
                mu_data = await get_military_unit(unit['id'], session)
            except Exception:
                mu_data = None
            if not mu_data:
                continue
            try:
                commanders = (mu_data.get('roles') or {}).get('commanders') or []
            except Exception:
                commanders = []
            for cid in commanders:
                if cid:
                    commander_ids.add(cid)

        # Build lookup maps for guild members
        members = list(guild.members)
        name_map = {m.name.lower(): m for m in members}
        display_map = {m.display_name.lower(): m for m in members}

        desired_members = set()
        added = []
        removed = []

        # For each commander api id, try to find the corresponding guild member
        for api_id in commander_ids:
            try:
                rec = get_record_by_api_id(api_id)
            except Exception:
                rec = None

            member = None
            if rec:
                discord_username = (rec.get('discord_username') or '').lower() if rec.get('discord_username') else None
                display_name = (rec.get('display_name') or '').lower() if rec.get('display_name') else None
                if discord_username and discord_username in name_map:
                    member = name_map[discord_username]
                elif display_name and display_name in display_map:
                    member = display_map[display_name]

            # If not found via DB, try to resolve via API username and match display_name
            if member is None:
                try:
                    info = await get_user_info(api_id, session)
                except Exception:
                    info = None
                if isinstance(info, dict):
                    username = (info.get('username') or '').lower()
                    if username and username in display_map:
                        member = display_map[username]
                        try:
                            save_user(member.name, member.display_name, api_id)
                        except Exception:
                            pass

            if member:
                desired_members.add(member)

        # Assign commander role to desired members
        for member in desired_members:
            if commander_role not in member.roles:
                try:
                    await member.add_roles(commander_role, reason="Assigned commander role from MU config")
                    added.append(member.display_name)
                except Exception:
                    pass

        # Remove commander role from members that should no longer have it
        current_with_role = commander_role.members if commander_role else []
        for member in current_with_role:
            if member not in desired_members:
                try:
                    await member.remove_roles(commander_role, reason="Removed commander role (no longer MU commander)")
                    removed.append(member.display_name)
                except Exception:
                    pass

        # Send a summary if there were any changes
        channel = guild.get_channel(config.get('channels', {}).get('reports')) if guild else None
        if channel and (len(added) > 0 or len(removed) > 0):
            embed = self.build_commander_embed(added, removed)
            if embed:
                try:
                    await channel.send(embed=embed)
                except Exception:
                    pass

    @commander_roles.before_loop
    async def before_commander_roles(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=6)
    async def unidentified_members(self):
        """Parses all members of the server that hold the citizen role and checks
           if their server nickname matches the one from the game.
        """
        guild = self.bot.get_guild(config['guild'])
        citizen = guild.get_role(config['roles']['citizen'])
        newbie = guild.get_role(config['roles']['newbie'])

        members = set()
        if citizen:
            members.update(citizen.members)
        if newbie:
            members.update(newbie.members)

        unidentified = []
        session = await get_shared_session()
        for member in members:
            user = await get_user(member.display_name, session)
            if user is None:
                # try to find api_id in local DB by display name or discord username
                try:
                    api_id = find_api_id_by_display_name(member.display_name) or find_api_id_by_discord_username(member.name)
                    if api_id:
                        info = await get_user_info(api_id, session)
                        if info:
                            # Prefer the API username as the authoritative display name
                            new_display = info.get('username') or None
                            # If the API reports a different display name, try to update the member's server nickname
                            if new_display and new_display != member.display_name:
                                try:
                                    await member.edit(nick=new_display, reason="Sync WarEra username")
                                except Exception:
                                    # ignore failures (permissions, hierarchy, etc.)
                                    pass
                            # update stored mapping with the current discord username and latest display name
                            save_user(member.name, new_display or member.display_name, api_id)
                            continue
                except Exception:
                    pass
                unidentified.append(member)
            else:
                try:
                    api_id = user.get('_id') if isinstance(user, dict) else None
                    if api_id:
                        save_user(member.name, member.display_name, api_id)
                except Exception as e:
                    pass
                    unidentified.append(member)
        if len(unidentified) == 0:
            return None
        # Always send an embed, even if there are no unidentified players
        channel = guild.get_channel(config["channels"]["reports"]) if guild else None
        if channel:
            embeds = self.build_unidentified_embed(unidentified)
            # builder returns a list of embeds; send them sequentially
            if isinstance(embeds, list):
                for e in embeds:
                    try:
                        await channel.send(embed=e)
                    except Exception:
                        pass
            else:
                try:
                    await channel.send(embed=embeds)
                except Exception:
                    pass

    @unidentified_members.before_loop
    async def before_unidentified_members(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def takeover_countries(self):
        """Parses all countries of the server and posts any country that can be taken over.
        """
        if self.countries is None:
            self.countries = await self.get_countries()

        guild = self.bot.get_guild(config['guild'])
        active_countries = config.get('active_countries', [])
        session = await get_shared_session()
        empty_countries = []
        countries_list = self.countries or []
        for country in countries_list:
            if active_countries is not None and len(active_countries) != 0:
                if country['name'] in active_countries:
                    continue
            government = await get_country_government(country['_id'], session)
            # country is empty, api displays only _id, country, __v, and congressMembers keys .
            if government is not None and len(government.keys()) == 4 and len(government['congressMembers']) == 0:
                empty_countries.append((country['name'], country['_id']))
        # Always send an embed reporting the results (may be empty)
        if len(empty_countries) == 0:
            return
        channel = guild.get_channel(config["channels"]["public"]) if guild else None
        if channel:
            embed = self.build_takeover_embed(empty_countries)
            await channel.send(embed=embed)

    @takeover_countries.before_loop
    async def before_takeover_countries(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=10)
    async def buff_monitor(self):
        """Checks fighter members for active buffs and notifies users when their
        buff is nearing expiration (within NOTIFY_THRESHOLD_MINUTES).
        The method uses an in-memory cache (`self.buff_check_cache`) to avoid
        scanning all fighters every run; entries store the earliest `next_check`.
        """
        guild = self.bot.get_guild(config['guild'])
        if guild is None:
            return
        fight_role = guild.get_role(config['roles']['fight'])
        if fight_role is None:
            return

        now = datetime.now(timezone.utc)
        members = fight_role.members if fight_role else []
        seen_api_ids = set()
        session = await get_shared_session()
        for member in members:
                api_id = None
                try:
                    api_id = find_api_id_by_display_name(member.display_name) or find_api_id_by_discord_username(member.name)
                except Exception:
                    api_id = None

                # If we already know the next time to check this API id, skip for now
                if api_id:
                    entry = self.buff_check_cache.get(api_id)
                    if entry:
                        next_check = entry.get('next_check')
                        if next_check and next_check > now:
                            seen_api_ids.add(api_id)
                            continue

                # Retrieve user info. Prefer get_user_info when we already have api_id
                user_obj = None
                if api_id:
                    try:
                        user_obj = await get_user_info(api_id, session)
                    except Exception:
                        user_obj = None

                if not user_obj:
                    user_obj = await get_user(member.display_name, session)
                    if isinstance(user_obj, dict):
                        api_id = user_obj.get('_id') or api_id
                        if api_id:
                            try:
                                save_user(member.name, member.display_name, api_id)
                            except Exception:
                                pass

                if not user_obj:
                    continue

                # Parse buff/debuff information from the user object
                buffs = user_obj.get('buffs') or {}
                buff_end_at = None
                buff_type = None
                buff_active = False
                if isinstance(buffs, dict) and buffs:
                    if 'debuffEndAt' in buffs and buffs.get('debuffEndAt'):
                        buff_end_at = buffs.get('debuffEndAt')
                        buff_type = 'Debuff'
                    elif 'buffEndAt' in buffs and buffs.get('buffEndAt'):
                        buff_end_at = buffs.get('buffEndAt')
                        buff_type = 'Buff'

                    if buff_end_at:
                        try:
                            buff_dt = datetime.fromisoformat(buff_end_at.replace('Z', '+00:00'))
                            remaining = buff_dt - now
                            buff_active = remaining.total_seconds() > 0
                        except Exception:
                            buff_active = False

                cache_entry = self.buff_check_cache.get(api_id, {})

                # No active buff/debuff
                if not buff_end_at or not buff_active:
                    cache_entry['next_check'] = now + timedelta(hours=DEFAULT_SKIP_HOURS)
                    cache_entry['notified_for_end_at'] = None
                    self.buff_check_cache[api_id] = cache_entry
                    seen_api_ids.add(api_id)
                    continue

                # Currently on debuff -> avoid until debuff ends
                if buff_type == 'Debuff':
                    cache_entry['next_check'] = buff_dt + timedelta(minutes=1)
                    cache_entry['notified_for_end_at'] = None
                    self.buff_check_cache[api_id] = cache_entry
                    seen_api_ids.add(api_id)
                    continue

                # Active buff: notify when within effective threshold (accounts for poll delay)
                remaining_seconds = (buff_dt - now).total_seconds()
                notified_token = cache_entry.get('notified_for_end_at')
                if remaining_seconds <= EFFECTIVE_NOTIFY_MINUTES * 60:
                    # Determine current health/hunger values (safe parsing)
                    skills = user_obj.get('skills') or {}
                    health = skills.get('health') or {}
                    hunger = skills.get('hunger') or {}
                    try:
                        health_curr = int(health.get('currentBarValue') or 0)
                    except Exception:
                        health_curr = 0
                    try:
                        hunger_curr = int(hunger.get('currentBarValue') or 0)
                    except Exception:
                        hunger_curr = 0

                    has_resources = (health_curr > 0) or (hunger_curr > 0)

                    # Check if a top-of-hour (o'clock) occurs between now and buff end —
                    # if so, health/hunger will be regenerated by 10% and we should notify.
                    next_top = now.replace(minute=0, second=0, microsecond=0)
                    if next_top <= now:
                        next_top = next_top + timedelta(hours=1)
                    oclock_within_window = next_top <= buff_dt

                    should_notify = has_resources or oclock_within_window

                    # Only send notification when conditions are met and we haven't
                    # already notified for this buff end timestamp.
                    if should_notify and notified_token != buff_end_at:
                        minutes = max(1, int(remaining_seconds // 60))
                        text = f"Hi {member.display_name}, your pill buff expires in about {minutes} minute{'s' if minutes != 1 else ''}. Please empty into a fight if possible."
                        try:
                            await member.send(text)
                        except Exception:
                            channel = guild.get_channel(config.get('channels', {}).get('public')) if guild else None
                            if channel:
                                try:
                                    await channel.send(f"{member.mention} — {text}")
                                except Exception:
                                    pass
                        cache_entry['notified_for_end_at'] = buff_end_at
                        cache_entry['next_check'] = buff_dt + timedelta(minutes=1)
                    else:
                        # Don't notify now — schedule a re-check after buff end
                        cache_entry['next_check'] = buff_dt + timedelta(minutes=1)
                    self.buff_check_cache[api_id] = cache_entry
                    seen_api_ids.add(api_id)
                    continue

                # Schedule next check at buff_dt - (effective threshold)
                next_check = buff_dt - timedelta(minutes=EFFECTIVE_NOTIFY_MINUTES)
                if next_check <= now:
                    next_check = now + timedelta(minutes=BUFF_MONITOR_INTERVAL_MINUTES)
                cache_entry['next_check'] = next_check
                cache_entry['notified_for_end_at'] = cache_entry.get('notified_for_end_at')
                self.buff_check_cache[api_id] = cache_entry
                seen_api_ids.add(api_id)

        # Prune cache entries for API ids we did not see during this run
        to_prune = [k for k in list(self.buff_check_cache.keys()) if k not in seen_api_ids]
        for k in to_prune:
            try:
                entry = self.buff_check_cache.get(k)
                if not entry:
                    del self.buff_check_cache[k]
                    continue
                next_check = entry.get('next_check')
                if not next_check or (isinstance(next_check, datetime) and next_check < datetime.now(timezone.utc) - timedelta(hours=24)):
                    del self.buff_check_cache[k]
            except Exception:
                pass

    @tasks.loop(minutes=BOUNTY_MONITOR_INTERVAL_MINUTES)
    async def bounty_monitor(self):
        """Checks active battles for bounties that are upcoming or currently active
        (moneyPool != 0 and bountyEffectiveAt present). Sends a summary embed
        to the public channel when any are found.
        """
        guild = self.bot.get_guild(config['guild'])
        if guild is None:
            return

        session = await get_shared_session()
        try:
            battles = await get_active_battles(session)
        except Exception:
            battles = None

        if not battles:
            return

        now = datetime.now(timezone.utc)
        # Collect battles that have a positive moneyPool on either side and a bountyEffectiveAt
        battles_with_bounty = []
        for battle in battles:
            bid = battle.get('_id') or battle.get('id') or battle.get('battleId') or None
            attacker = battle.get('attacker') or {}
            defender = battle.get('defender') or {}

            try:
                atk_pool = float(attacker.get('moneyPool') or 0)
            except Exception:
                atk_pool = 0.0
            try:
                def_pool = float(defender.get('moneyPool') or 0)
            except Exception:
                def_pool = 0.0

            try:
                atk_money = float(attacker.get('moneyPer1kDamages') or 0)
            except Exception:
                atk_money = 0.0
            try:
                def_money = float(defender.get('moneyPer1kDamages') or 0)
            except Exception:
                def_money = 0.0

            atk_bounty_at = attacker.get('bountyEffectiveAt')
            def_bounty_at = defender.get('bountyEffectiveAt')

            # Only consider pools strictly greater than 0
            if (atk_pool > 0 and atk_bounty_at and atk_money >= 0.1) or (def_pool > 0 and def_bounty_at and def_money >= 0.1):
                battles_with_bounty.append({
                    'battle': battle,
                    'id': bid,
                    'attacker_pool': atk_pool,
                    'defender_pool': def_pool,
                    'attacker_bounty_at': atk_bounty_at,
                    'defender_bounty_at': def_bounty_at,
                })

        if not battles_with_bounty:
            return

        # Fetch country names for all referenced country ids
        country_cache: dict = {}
        async def resolve_country(cid):
            if not cid:
                return None
            if cid in country_cache:
                return country_cache[cid]
            try:
                cobj = await get_country(cid, session)
            except Exception:
                cobj = None
            if isinstance(cobj, dict):
                name = cobj.get('name') or cid
            else:
                name = cid
            country_cache[cid] = name
            return country_cache[cid]

        # Resolve countries used in the selected battles
        tasks_resolve = []
        for entry in battles_with_bounty:
            b = entry['battle']
            atk_cid = (b.get('attacker') or {}).get('country')
            def_cid = (b.get('defender') or {}).get('country')
            if atk_cid:
                tasks_resolve.append(resolve_country(atk_cid))
            if def_cid:
                tasks_resolve.append(resolve_country(def_cid))
        # run resolves
        await asyncio.gather(*tasks_resolve)

        # For each side with a positive pool, send a single embed if it's new/changed
        channel = guild.get_channel(config.get('channels', {}).get('public')) if guild else None
        current_keys = set()
        for entry in battles_with_bounty:
            b = entry['battle']
            bid = entry['id'] or 'unknown'
            atk = b.get('attacker') or {}
            dfn = b.get('defender') or {}
            atk_cid = atk.get('country')
            def_cid = dfn.get('country')
            atk_name = country_cache.get(atk_cid, atk_cid or 'unknown')
            def_name = country_cache.get(def_cid, def_cid or 'unknown')

            # attacker side: send a simple plain-text message instead of an embed
            if entry['attacker_pool'] > 0 and entry['attacker_bounty_at']:
                key = f"{bid}:attacker"
                current_keys.add(key)
                prev = self.displayed_bounties.get(key)
                if prev != entry['attacker_bounty_at']:
                    try:
                        money_per = float(atk.get('moneyPer1kDamages') or 0)
                    except Exception:
                        money_per = 0.0
                    pool = round(float(entry['attacker_pool']), 2)
                    battle_link = f"https://app.warera.io/battle/{bid}"
                    # Format: "moneyPer/pool from <country_A> (Attacker) against <country_B> (Defender) — View battle: <link>"
                    msg = f"{money_per}/{pool} from {atk_name} (Attacker) against {def_name} (Defender) — [View Battle]({battle_link})"
                    if channel:
                        try:
                            sent = await channel.send(msg)
                            await sent.edit(suppress=True)
                        except Exception:
                            pass
                    self.displayed_bounties[key] = entry['attacker_bounty_at']

            # defender side: send a simple plain-text message instead of an embed
            if entry['defender_pool'] > 0 and entry['defender_bounty_at']:
                key = f"{bid}:defender"
                current_keys.add(key)
                prev = self.displayed_bounties.get(key)
                if prev != entry['defender_bounty_at']:
                    try:
                        money_per = float(dfn.get('moneyPer1kDamages') or 0)
                    except Exception:
                        money_per = 0.0
                    pool = round(float(entry['defender_pool']), 2)
                    battle_link = f"https://app.warera.io/battle/{bid}"
                    # Format: "moneyPer/pool from <country_A> (Defender) against <country_B> (Attacker) — View battle: <link>"
                    msg = f"**[BOUNTY]** {money_per}/{pool} from {def_name} (Defender) against {atk_name} (Attacker) — [View Battle]({battle_link})"
                    if channel:
                        try:
                            sent = await channel.send(msg)
                            await sent.edit(suppress=True)
                        except Exception:
                            pass
                    self.displayed_bounties[key] = entry['defender_bounty_at']

        # prune displayed_bounties keys for battles that are no longer active
        active_ids = set()
        for b in battles:
            bid = b.get('_id') or b.get('id') or b.get('battleId') or None
            if bid:
                active_ids.add(str(bid))

        to_remove = [k for k in list(self.displayed_bounties.keys()) if k.split(':')[0] not in active_ids]
        for k in to_remove:
            try:
                del self.displayed_bounties[k]
            except Exception:
                pass

    @bounty_monitor.before_loop
    async def before_bounty_monitor(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=MERCENARY_MONITOR_INTERVAL_MINUTES)
    async def mercenary_monitor(self):
        """Checks active mercenary contract auctions and posts new/changed ones.

        Message format: "<country_name> posted a <initialPerK>/<budget> contract for <forCountrySide> side."
        """
        guild = self.bot.get_guild(config['guild'])
        if guild is None:
            return

        session = await get_shared_session()
        try:
            auctions = await get_mercenary_auctions(session)
        except Exception:
            auctions = None

        if not auctions:
            return

        channel = guild.get_channel(config.get('channels', {}).get('contracts')) if guild else None
        if channel is None:
            return

        # simple cache for country names
        country_cache: dict = {}
        async def resolve_country(cid):
            if not cid:
                return 'unknown'
            if cid in country_cache:
                return country_cache[cid]
            try:
                cobj = await get_country(cid, session)
            except Exception:
                cobj = None
            name = cobj.get('name') if isinstance(cobj, dict) else str(cid)
            country_cache[cid] = name
            return name

        seen_ids = set()
        for a in auctions:
            aid = a.get('_id')
            if not aid:
                continue
            seen_ids.add(aid)

            status = a.get('status')
            # only post active auctions
            if status != 'active':
                continue

            # token to detect changes (prefer updatedAt, fallback to createdAt or currentPayout)
            token = a.get('updatedAt') or a.get('createdAt') or a.get('currentPayout')
            prev = self.displayed_auctions.get(aid)
            if prev == token:
                continue

            country_name = await resolve_country(a.get('country') or a.get('forCountry'))
            initial = a.get('initialPerK')
            budget = a.get('budget')
            side = a.get('forCountrySide') or a.get('forCountrySide')
            battle_link = f"https://app.warera.io/battle/{a.get('battle')}"
            text = f"**[CONTRACT]** {country_name} posted a {initial}/{budget} contract for {side} side — [View Battle]({battle_link})"
            try:
                sent = await channel.send(text)
                await sent.edit(suppress=True)
            except Exception:
                pass

            # record token
            self.displayed_auctions[aid] = token

        # prune auctions that are no longer active
        to_prune = [k for k in list(self.displayed_auctions.keys()) if k not in seen_ids]
        for k in to_prune:
            try:
                del self.displayed_auctions[k]
            except Exception:
                pass

    @mercenary_monitor.before_loop
    async def before_mercenary_monitor(self):
        await self.bot.wait_until_ready()

    def build_bounty_embed(self, items: list) -> discord.Embed:
        if not items:
            embed = discord.Embed(
                title="Bounty Check",
                description="No active or upcoming bounties found.",
                color=discord.Color.green()
            )
            embed.set_footer(text="Total: 0")
            return embed

        embed = discord.Embed(
            title="Active / Upcoming Bounties",
            description="Battles with non-empty bounty pools:",
            color=discord.Color.orange()
        )

        lines = []
        for it in items:
            bid = it.get('battle_id') or 'unknown'
            side = it.get('side')
            country = it.get('country') or 'unknown'
            pool = it.get('moneyPool')
            effective = it.get('effectiveAt')
            lines.append(f"* Battle {bid} — {side} — Pool: {pool} — Effective: {effective} (country: {country})")

        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 1000:
                embed.add_field(name="Bounties", value=chunk, inline=False)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            embed.add_field(name="Bounties", value=chunk, inline=False)

        embed.set_footer(text=f"Total: {len(items)}")
        return embed

    def build_takeover_embed(self, countries) -> discord.Embed:
        if not countries:
            embed = discord.Embed(
                title="Takeover Countries Check",
                description="No takeover countries were found.",
                color=discord.Color.green()
            )
            embed.set_footer(text="Total: 0")
            return embed

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
        
    def build_unidentified_embed(self, members: list[discord.Member]) -> list:
        """Return a list of embeds (one or more) that together list unidentified members.
        Splits content so no single embed exceeds Discord's embed size limits.
        """
        if not members:
            embed = discord.Embed(
                title="Unidentified Players Check",
                description="No unidentified players were found.",
                color=discord.Color.green()
            )
            embed.set_footer(text="Total: 0")
            return [embed]

        lines = [f"* {m.display_name} ('{m.id}')" for m in members]

        # First split into field-sized chunks (<=1000 chars per field)
        field_chunks: list[str] = []
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 1000:
                field_chunks.append(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            field_chunks.append(chunk)

        # Now group fields into embeds without exceeding a safe embed size limit
        EMBED_CHAR_LIMIT = 5800  # keep some headroom under 6000
        title = "Unidentified Players Found"
        description = "The following members could not be matched:"

        embeds: list[discord.Embed] = []
        current_embed = discord.Embed(title=title, description=description, color=discord.Color.orange())
        current_length = len(title) + len(description)

        for field_value in field_chunks:
            field_name = "Players"
            field_len = len(field_name) + len(field_value)
            # Start a new embed if adding this field would exceed the safe limit
            if current_length + field_len > EMBED_CHAR_LIMIT and len(current_embed.fields) > 0:
                current_embed.set_footer(text=f"Total: {len(members)}")
                embeds.append(current_embed)
                current_embed = discord.Embed(title=title, description=description, color=discord.Color.orange())
                current_length = len(title) + len(description)

            current_embed.add_field(name=field_name, value=field_value, inline=False)
            current_length += field_len

        # Append last embed
        current_embed.set_footer(text=f"Total: {len(members)}")
        embeds.append(current_embed)
        return embeds
    
    def build_skill_roles_embed(self, stats: dict) -> discord.Embed:
        economy_added = stats.get('economy_added', [])
        economy_removed = stats.get('economy_removed', [])
        fight_added = stats.get('fight_added', [])
        fight_removed = stats.get('fight_removed', [])
        total = len(economy_added) + len(economy_removed) + len(fight_added) + len(fight_removed)

        # If there are no changes, return None so callers can skip sending an embed
        if total == 0:
            return None

        embed = discord.Embed(
            title="Skill Roles Updated",
            description="Summary of skill role changes:",
            color=discord.Color.orange()
        )

        def format_list(lst: list) -> str:
            if not lst:
                return "None"
            lines = [f"* {n}" for n in lst]
            cur = ""
            count = 0
            for line in lines:
                if len(cur) + len(line) + 1 > 1000:
                    break
                cur += line + "\n"
                count += 1
            remaining = len(lines) - count
            if remaining > 0:
                cur = cur.rstrip("\n")
                cur += f"\n... and {remaining} more"
            return cur

        embed.add_field(name="Economy Roles — Added", value=format_list(economy_added), inline=False)
        embed.add_field(name="Economy Roles — Removed", value=format_list(economy_removed), inline=False)
        embed.add_field(name="Fight Roles — Added", value=format_list(fight_added), inline=False)
        embed.add_field(name="Fight Roles — Removed", value=format_list(fight_removed), inline=False)
        embed.set_footer(text=f"Total changes: {total}")
        return embed

    def build_military_unit_embed(self, added: dict, removed: dict) -> discord.Embed:
        all_roles = set(list(added.keys()) + list(removed.keys()))
        total = sum(len(v) for v in added.values()) + sum(len(v) for v in removed.values())

        if total == 0:
            return None

        embed = discord.Embed(
            title="Military Unit Roles Updated",
            description="Summary of military unit role changes:",
            color=discord.Color.orange()
        )

        def format_players(lst: list) -> str:
            if not lst:
                return None
            lines = [f"* {n}" for n in lst]
            cur = ""
            count = 0
            for line in lines:
                if len(cur) + len(line) + 1 > 1000:
                    break
                cur += line + "\n"
                count += 1
            remaining = len(lines) - count
            if remaining > 0:
                cur = cur.rstrip("\n")
                cur += f"\n... and {remaining} more"
            return cur

        for role_name in sorted(all_roles):
            a_list = added.get(role_name, [])
            r_list = removed.get(role_name, [])
            a_formatted = format_players(a_list)
            r_formatted = format_players(r_list)
            if a_formatted is None and r_formatted is None:
                continue
            if a_formatted is not None:
                embed.add_field(name=role_name, value=f"Added:\n{a_formatted}\n", inline=False)
            if r_formatted is not None:
                embed.add_field(name=role_name, value=f"Removed:\n{r_formatted}\n", inline=False)
        embed.set_footer(text=f"Total changes: {total}")
        return embed

    def build_commander_embed(self, added: list, removed: list) -> discord.Embed:
        total = len(added) + len(removed)
        if total == 0:
            return None
        embed = discord.Embed(
            title="Commander Roles Updated",
            description="Summary of commander role synchronization:",
            color=discord.Color.orange()
        )
        def fmt(lst: list) -> str:
            if not lst:
                return "None"
            lines = [f"* {n}" for n in lst]
            cur = ""
            count = 0
            for line in lines:
                if len(cur) + len(line) + 1 > 1000:
                    break
                cur += line + "\n"
                count += 1
            remaining = len(lines) - count
            if remaining > 0:
                cur = cur.rstrip("\n")
                cur += f"\n... and {remaining} more"
            return cur

        embed.add_field(name="Added", value=fmt(added), inline=False)
        embed.add_field(name="Removed", value=fmt(removed), inline=False)
        embed.set_footer(text=f"Total changes: {total}")
        return embed
    
async def setup(bot: commands.Bot):
    await bot.add_cog(Jobs(bot))