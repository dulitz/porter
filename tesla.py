# tesla.py
#
# the Tesla module for porter, the Prometheus exporter.
#
# see https://github.com/tdorssers/TeslaPy
# and (unused here) https://github.com/mlowijs/tesla_api

import json, logging, prometheus_client, teslapy, time, threading

from dateutil.parser import isoparse
from prometheus_client.core import GaugeMetricFamily

LOGGER = logging.getLogger('porter.tesla')

REQUEST_TIME = prometheus_client.Summary('tesla_processing_seconds',
                                         'time of tesla requests')


class TeslaClient:
    def __init__(self, config, password='', passcode_getter=None, factor_selector=None):
        self.config = config
        self.cv = threading.Condition()
        myconfig = config.get('tesla')
        if not myconfig:
            raise Exception('no tesla configuration')
        user = myconfig.get('user')
        if not user:
            raise Exception('no tesla user')
        if not factor_selector:
            factor_selector = lambda factorlist: factorlist[0]
        verify = myconfig.get('verify', True)
        proxy = myconfig.get('proxy', '')
        self.client = teslapy.Tesla(user, password, passcode_getter=passcode_getter,
                                    factor_selector=factor_selector,
                                    verify=verify, proxy=proxy)
        self.client.fetch_token()

    @REQUEST_TIME.time()
    def collect(self, target):
        """For each battery, emit all the data. For each vehicle, find if it is online and,
        if it is, emit all the data. An offline vehicle is awakened only if its VIN is
        specifically a target.
        """
        gmflist = []
        with self.cv:
            self.client.fetch_token()
            for v in self.client.vehicle_list():
                summary = v.get_vehicle_summary()
                if summary.get('state', '').lower() == 'online' or target == summary.get('vin'):
                    gmflist += self._collect_vehicle(v.get_vehicle_data())
                else:
                    gmflist += self._collect_vehicle(summary)
            for b in self.client.battery_list():
                gmflist += self._collect_battery(b.get_battery_data())
        return gmflist

    def _collect_vehicle(self, vdata):
        metric_to_gauge = {}
        def makegauge(metric, desc, morelabels=[]):
            already = metric_to_gauge.get(metric)
            if already:
                return already
            labels = ['vin', 'name', 'api'] + morelabels
            gmf = GaugeMetricFamily(metric, desc, labels=labels)
            metric_to_gauge[metric] = gmf
            return gmf

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
        self._walk(vdata, makegauge, commonlabels, xlatemap)

        return metric_to_gauge.values()

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
            
    def _walk(self, d, makegauge, commonlabels, xlatemap):
        for (k, v) in d.items():
            k = xlatemap.get(k, k)
            if k is None:
                continue
            if isinstance(v, dict):
                mylabels = commonlabels[:]
                mylabels[2] = k
                self._walk(v, makegauge, mylabels, xlatemap)
            elif isinstance(v, (int, float)): # int includes bool
                g = makegauge(k, 'auto-generated by porter/tesla.py')
                g.add_metric(commonlabels, v)
            elif k == 'solar':
                g = makegauge('has_solar', '1 if has solar, 0 otherwise', ['kind'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [d.get('solar_type', '')], v)
            elif k == 'grid_status':
                g = makegauge('grid_is_active', '1 if state is Active, 0 otherwise', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'active' else 0)
            elif k == 'default_real_mode':
                g = makegauge('default_operating_mode_is_selfconsumption', '', ['mode'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'self_consumption' else 0)
            elif k == 'operation':
                g = makegauge('operating_mode_is_selfconsumption', '', ['mode'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'self_consumption' else 0)
            elif k == 'state':
                g = makegauge('is_online', '1 if state is online, 0 otherwise', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'online' else 0)
            elif k == 'charging_state':
                g = makegauge('is_charging', '1 if state is charging, 0 otherwise', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 1 if v == 'charging' else 0)
            elif k == 'conn_charge_cable':
                g = makegauge('charge_cable_connector_is_sae', '1 if connector is SAE, 0 otherwise', ['state'])
                g.add_metric(commonlabels + [v], 1 if v == 'SAE' else 0)
            elif k == 'climate_keeper_mode':
                g = makegauge('climate_keeper_is_on', '1 if mode is not off, 0 if it is off', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 0 if v == 'off' else 1)
            elif k == 'shift_state':
                g = makegauge(k, '1 if mode is not off, 0 if it is off', ['state'])
                v = (v or '').lower()
                g.add_metric(commonlabels + [v], 0 if v == 'off' else 1)
            elif k == 'sun_roof_state':
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
# copy that token into your Docker image.

if __name__ == '__main__':
    import getpass, sys, yaml
    assert len(sys.argv) == 2, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    #password = getpass.getpass('Password for Tesla account:')
    password = ''

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

    client = TeslaClient(config, password, factor_selector=factor_selector,
                         passcode_getter=passcode_getter)
    for v in client.client.vehicle_list():
        print(v.get_vehicle_summary())
        print(v.get_vehicle_data())
    for b in client.client.battery_list():
        print(str(b))
