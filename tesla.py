"""
tesla.py

The Tesla module for porter, the Prometheus exporter.

See https://github.com/tdorssers/TeslaPy
and (unused here) https://github.com/mlowijs/tesla_api
"""

import json, logging, os, prometheus_client, teslapy, time, threading

from dateutil.parser import isoparse
from prometheus_client.core import GaugeMetricFamily

LOGGER = logging.getLogger('porter.tesla')

REQUEST_TIME = prometheus_client.Summary('tesla_processing_seconds',
                                         'time of tesla requests')


def _authcache_valid_for(cache, email):
    token = cache.get(email, {}).get('ownerapi', {})
    if not token:
        return False
    now = time.time()
    expires_at = token['created_at'] + token['expires_in']
    return now < expires_at

class TeslaClient:
    def __init__(self, config, password='', passcode_getter=None, factor_selector=None):
        self.config = config
        self.cv = threading.Condition()
        myconfig = config.get('tesla')
        if not myconfig:
            raise Exception('no tesla configuration')
        self.vehicle_cache = {}
        self.vehicle_cache_time = myconfig.get('vehiclecachetime', 60*60)
        users = myconfig.get('users')
        if not users:
            raise Exception('no tesla users')
        if not factor_selector: # then always select the first factor
            factor_selector = lambda factorlist: factorlist[0]
        verify = myconfig.get('verify', True)
        proxy = myconfig.get('proxy', '')
        self.usertoclient = {}
        for user in users:
            try:
                c = teslapy.Tesla(
                    user, password, passcode_getter=passcode_getter,
                    factor_selector=factor_selector, verify=verify, proxy=proxy
                )
                c.fetch_token()
            except ValueError: # could not authenticate
                LOGGER.error(f'cache.json in {os.getcwd()} did not contain a valid token and we could not authenticate {user}')
                raise
            self.usertoclient[user] = c
            LOGGER.info(f'successfully authenticated {user}')


    @REQUEST_TIME.time()
    def collect(self, target):
        """For each battery, emit all the data. For each vehicle, find if it is online and,
        if it is, emit all the data. An offline vehicle is awakened only if its VIN is
        specifically a target.
        """
        with self.cv:
            assert self.usertoclient
            return self._collect_locked(target)

    def _collect_locked(self, target):
        gmflist = []
        for (user, client) in self.usertoclient.items():
            client.fetch_token() # refresh our token if needed
            for v in client.vehicle_list():
                gmflist += self._cache_or_collect_vehicle(target, v)
            for b in client.battery_list():
                gmflist += self._collect_battery(b.get_battery_data())
        return gmflist

    def _cache_or_collect_vehicle(self, target, v):
        summary = v.get_vehicle_summary()
        vkey = v['id_s']  # Vehicle is not hashable
        now = time.time()

        if target == summary.get('vin'):
            # then we were given the vin directly, so get fresh data even
            # if we wake the car up or keep it from going to sleep
            v.sync_wake_up()
            (cache, awake) = self._collect_vehicle(v.get_vehicle_data())
            self.vehicle_cache[vkey] = (now, cache, awake)
            return cache

        if summary.get('state', '').lower() == 'online':
            # This is the complicated case: we want to gather data
            # opportunistically, but not so often we keep the vehicle
            # awake. If it is online and we have no cache, or if it is
            # powered up, then something else awakened it and we should grab
            # data now. If our cache is too old, we refresh. Otherwise we
            # return cached data to make sure we don't keep the vehicle awake.
            (cachetime, cache, awake) = self.vehicle_cache.get(vkey, (0, None, True))
            if now - cachetime <= self.vehicle_cache_time and not awake:
                return cache
            LOGGER.debug(f'refreshing cache for {"awake " if awake else ""}{vkey}')
            try:
                (cache, awake) = self._collect_vehicle(v.get_vehicle_data())
                self.vehicle_cache[vkey] = (now, cache, awake)
                return cache
            except teslapy.HTTPError as e:
                if e.response.status_code != 408:
                    raise
                # if it was 408, then fall through since it isn't really online
                LOGGER.debug(f'status 408 for {vkey}: using summary')

        # If we get here, it's because the car is offline.
        # We invalidate the cache because when the car comes back online
        # we should get fresh data, as someone else woke it up.
        (gmf_summary, awake) = self._collect_vehicle(summary)
        if self.vehicle_cache.get(vkey, (0, None, True))[0]:
            LOGGER.debug(f'invalidating cache for offline {vkey}')
        self.vehicle_cache[vkey] = (0, None, True)
        return gmf_summary

    def _collect_vehicle(self, vdata):
        vehicle_awake = False
        metric_to_gauge = {}
        def makegauge(metric, desc, morelabels=[]):
            already = metric_to_gauge.get(metric)
            if already:
                return already
            labels = ['vin', 'name', 'api'] + morelabels
            gmf = GaugeMetricFamily(metric, desc, labels=labels)
            metric_to_gauge[metric] = gmf
            return gmf

        def registervalue(metricname, v):
            nonlocal vehicle_awake
            LOGGER.debug(f'observing register {metricname} with value {v}')
            if metricname == 'power' and v != 0:
                LOGGER.debug(f'{vdata.get("display_name", "vehicle")} is awake with power {v}')
                vehicle_awake = True
            elif metricname == 'charging_state' and v == 'charging':
                LOGGER.debug(f'{vdata.get("display_name", "vehicle")} is awake with state {v}')
                vehicle_awake = True

        xlatemap = {
            'id': None,
            'vehicle_id': None,
            'backseat_token': None,
            'user_id': None,
            'battery_level': 'battery_level_pct',
            'battery_range': 'battery_range_mi',
            'charge_current_request': 'charge_current_request_a',
            'charge_energy_added': 'charge_energy_added_kw',
            'charge_limit_soc': 'charge_limit_soc_pct',
            'charge_miles_added_ideal': 'charge_distance_added_ideal_mi',
            'charge_miles_added_rated': 'charge_distance_added_rated_mi',
            'charge_rate': 'charge_rate_mph',
            'charger_actual_current': 'charger_a',
            'charger_pilot_current': 'charger_pilot_a',
            'charger_power': 'charger_power_kw',
            'est_battery_range': 'est_battery_range_mi',
            'ideal_battery_range': 'ideal_battery_range_mi',
            'minutes_to_full_charge': 'time_to_full_charge_min',
            'time_to_full_charge': None,
            'usable_battery_level': 'usable_battery_pct',
            'driver_temp_setting': 'driver_temp_setting_c',
            'inside_temp': 'inside_temp_c',
            'max_avail_temp': 'max_avail_temp_c',
            'min_avail_temp': 'min_avail_temp_c',
            'outside_temp': 'outside_temp_c',
            'passenger_temp_setting': 'passenger_temp_setting_c',
            'gps_as_of': 'location_timestamp',
            'power': 'power_kw',
            'df': 'driver_front_door_state',
            'dr': 'driver_rear_door_state',
            'pf': 'passenger_front_door_state',
            'pr': 'passenger_rear_door_state',
            'fd_window': 'driver_front_window_state',
            'rd_window': 'driver_rear_window_state',
            'fp_window': 'passenger_front_window_state',
            'rp_window': 'passenger_rear_window_state',
            'ft': 'front_trunk_state',
            'rt': 'rear_trunk_state',
            'sun_roof_percent_open': 'sunroof_open_pct',
            }
        commonlabels = [vdata.get('vin', ''), vdata.get('display_name', ''), '']
        gmf = makegauge('data_fetch_time', 'when this vehicle data was fresh')
        gmf.add_metric(commonlabels, time.time())
        self._walk(vdata, makegauge, commonlabels, xlatemap, registervalue=registervalue)

        return (metric_to_gauge.values(), vehicle_awake)

    def _collect_battery(self, bdata):        
        metric_to_gauge = {}
        def makegauge(metric, desc, morelabels=[]):
            already = metric_to_gauge.get(metric)
            if already:
                return already
            labels = ['id', 'name', 'api'] + morelabels
            gmf = GaugeMetricFamily(metric, desc, labels=labels)
            metric_to_gauge[metric] = gmf
            return gmf
        def makecomponentgauge(metric, desc, morelabels=[]):
            return makegauge(f'components_{metric}', desc, morelabels)
        
        xlatemap = {
            'id': None,
            'site_name': None,
            'energy_site_id': None,
            'energy_left': 'energy_remaining_kwh',
            'total_pack_energy': 'pack_energy_kwh',
            'percentage_charged': 'battery_soc_pct',
            'solar_type': None,
            'backup_reserve_percent': 'backup_reserve_pct',
            'load_power': 'load_consumption_w',
            'solar_power': 'solar_generation_w',
            'grid_power': 'grid_consumption_w',
            'battery_power': 'battery_generation_w',
            'generator_power': 'spinning_generation_w',
            }
        commonlabels = [bdata.get('id', ''), bdata.get('site_name', ''), '']
        components = bdata.get('components', {})
        if components:
            self._walk(components, makecomponentsgauge, commonlabels, xlatemap)
            del bdata['components']
        self._walk(bdata, makegauge, commonlabels, xlatemap)

        return metric_to_gauge.values()
            
    def _walk(self, d, makegauge, commonlabels, xlatemap, registervalue=lambda k, v: k):
        for (k, v) in d.items():
            k = xlatemap.get(k, k)
            if k is None:
                continue
            if isinstance(v, dict):
                mylabels = commonlabels[:]
                mylabels[2] = k
                self._walk(v, makegauge, mylabels, xlatemap, registervalue)
            elif isinstance(v, (int, float)): # int includes bool
                g = makegauge(k, 'auto-generated by porter/tesla.py')
                g.add_metric(commonlabels, v)
                registervalue(k, v)
            elif k == 'active':
                registervalue(k, v)
                if mylabels[2] == 'speed_limit_mode':
                    g = makegauge('speed_limit_mode', '1 if speed limited, 0 otherwise')
                    g.add_metric(commonlabels, v)
            elif k == 'solar':
                registervalue(k, v)
                g = makegauge('has_solar', '1 if has solar, 0 otherwise', ['kind'])
                g.add_metric(commonlabels + [d.get('solar_type', '')], v)
            elif k == 'grid_status':
                registervalue(k, v)
                g = makegauge('grid_is_active', '1 if state is Active, 0 otherwise', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'active' else 0)
            elif k == 'default_real_mode':
                g = makegauge('default_operating_mode_is_selfconsumption', '', ['mode'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'self_consumption' else 0)
            elif k == 'operation':
                registervalue(k, v)
                g = makegauge('operating_mode_is_selfconsumption', '', ['mode'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'self_consumption' else 0)
            elif k == 'state':
                registervalue(k, v)
                g = makegauge('is_online', '1 if state is online, 0 otherwise', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'online' else 0)
            elif k == 'charging_state':
                registervalue(k, v)
                g = makegauge('is_charging', '1 if state is charging, 0 otherwise', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'charging' else 0)
            elif k == 'conn_charge_cable':
                registervalue(k, v)
                g = makegauge('charge_cable_connector_is_sae', '1 if connector is SAE, 0 otherwise', ['state'])
                g.add_metric(commonlabels + [v], 1 if v == 'SAE' else 0)
            elif k == 'climate_keeper_mode':
                registervalue(k, v)
                g = makegauge('climate_keeper_is_on', '1 if mode is not off, 0 if it is off', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 0 if v == 'off' else 1)
            elif k == 'shift_state':
                registervalue(k, v)
                g = makegauge(k, '1 if mode is not off, 0 if it is off', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 0 if v == 'off' else 1)
            elif k == 'sun_roof_state':
                registervalue(k, v)
                g = makegauge('sunroof_closed', '1 if closed, 0 otherwise', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'closed' else 0)
            else:
                # it's a string or a list
                if k == 'timestamp':
                    g = makegauge(k, 'auto-generated by porter/tesla.py')
                    g.add_metric(commonlabels, isoparse(v).timestamp())
                else:
                    pass # ignore other strings
                    

# Run this as a main program to fetch the initial token into cache.json. You can then
# copy that token to your Docker image.

if __name__ == '__main__':
    import getpass, sys, yaml
    assert len(sys.argv) == 2, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))

    def passcode_getter():
        return raw_input('Passcode: ')
    def factor_selector(factors):
        while True:
            for (n, f) in enumerate(factors):
                print(f'{n}: {f}')
            n = input('Which one? ')
            try:
                return factors[int(n)]
            except e:
                print(e)

    myconfig = config['tesla']
    verify = myconfig.get('verify', True)
    proxy = myconfig.get('proxy', '')
    try:
        with open('cache.json', 'r') as cfile:
            cache = json.load(cfile)
    except FileNotFoundError:
        cache = {}
        print(f'no cache found, authenticating all users {myconfig["users"]}')
    for user in myconfig['users']:
        if _authcache_valid_for(cache, user):
            try:
                c = teslapy.Tesla(user, '', passcode_getter=passcode_getter,
                                  factor_selector=factor_selector,
                                  verify=verify, proxy=proxy)
                c.fetch_token()
                continue
            except ValueError:
                pass
        # cache is invalid; we need a password
        password = getpass.getpass(f'Password for {user}: ')
        c = teslapy.Tesla(user, password, passcode_getter=passcode_getter,
                          factor_selector=factor_selector,
                          verify=verify, proxy=proxy)
        c.fetch_token()

    client = TeslaClient(config, '')
    for c in client.usertoclient.values():
        for v in c.vehicle_list():
            print(v.get_vehicle_summary())
            print(v.get_vehicle_data())
        for b in c.battery_list():
            print(str(b))
