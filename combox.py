"""
combox.py

The Schneider Conext Combox module for porter, the Prometheus exporter.

Tested with
   webapp      version 2.0.874 built 2018/01/08 11:46:23
   application version Ver03.08BN0874 built 2018-01-08_11-46-08
   bootloader  version Ver01.04BN0128 built 2016-06-21_17-55-06

Since this isn't a documented API and I just hacked it from looking at the
internals of the webapp, it would ordinarily be considered "brittle." But
the Combox is also discontinued and out of maintenance, so I wouldn't
expect there to be any other firmware releases.

See Modbus Map for Schneider Conext XW: http://solar.schneider-electric.com/wp-content/uploads/2014/08/conext-modbus-map-conext-xw-device-503-0246-01-01_reva-3_eng.pdf
   and https://41j5tc3akbrn3uezx5av0jj1bgm-wpengine.netdna-ssl.com/wp-content/uploads/2018/05/ML20180401_Conext-Battery-Monitor-Owners-Guide-975_0691_01_01_Rev-D_ENG.pdf
   and http://solar.schneider-electric.com/wp-content/uploads/2014/05/503-0247-01-01_RevA.1_Modbus_Map_AGS_Device.pdf
"""

import json, logging, requests, prometheus_client, time, threading

from json.decoder import JSONDecodeError
from prometheus_client.core import GaugeMetricFamily


LOGGER = logging.getLogger('porter.combox')

REQUEST_TIME = prometheus_client.Summary('combox_processing_seconds',
                                         'time of combox requests')
CRITICAL_ENTRY = prometheus_client.Gauge('combox_critical_section_entry_time',
                                         'when the critical section was entered')
LOGIN_ATTEMPTS = prometheus_client.Gauge('combox_login_attempts',
                                         'how many times we have logged in to combox')

class ComboxError(Exception):
    pass

class ComboxClient:
    def __init__(self, config):
        self.config = config
        myconfig = config.get('combox')
        if not myconfig:
            raise ComboxError('no config for combox')
        self.user = myconfig.get('user')
        if not self.user:
            raise ComboxError('no user for combox')
        self.password = myconfig.get('password')
        if not self.password:
            raise ComboxError('no password for combox')
        if myconfig.get('timeout') is None:
            myconfig['timeout'] = 20
        self.target_to_client = {}
        self.cv = threading.Condition()


    @REQUEST_TIME.time()
    def collect(self, target):
        """request all the matching devices and get the status of each one"""

        metric_to_gauge = {}
        def makegauge(metric, desc, morelabels=[]):
            already = metric_to_gauge.get(metric)
            if already:
                return already
            labels = ['name', 'deviceId'] + morelabels
            gmf = GaugeMetricFamily(metric, desc, labels=labels)
            metric_to_gauge[metric] = gmf
            return gmf

        with self.cv:
            CRITICAL_ENTRY.set_to_current_time()
            clientdevices = self.target_to_client.get(target)
            if clientdevices:
                client, devices = clientdevices
            else:
                client = ComboxWeb(target, timeout=self.config['combox']['timeout'])
                client.login(self.user, self.password)
                devices = client.get_devicelist()
                self.target_to_client[target] = (client, devices)
            devinfos = [client.get_deviceinfo(d) for d in devices]
            CRITICAL_ENTRY.set(0)

        if not devinfos:
            raise ComboxError('got empty devicelist')

        for d in devinfos:
            if not d:
                continue # SCP returns an empty info dictionary
            labelvalues = [d['DeviceName'], d['UniqueIDNumber']]
            infolabels = ['fga', 'firmware', 'modbus_addr', 'serial']
            infovalues = [str(d.get('FGANumber', '')), str(d.get('FirmwareVersion', '')),
                          str(d.get('MBAddr', '')), str(d.get('SerialNumber', ''))]
            g = makegauge('combox_info', 'attribute information association',
                          morelabels=infolabels)
            g.add_metric(labelvalues + infovalues, 1)

            qual = {
                0: 'not qualifying',
                1: 'qualifying',
                2: 'missing',
                3: 'too low',
                4: 'too high',
                5: 'good'
                }
            xlate = {
                'Active': ('is_active', '1 if device active, 0 otherwise'),
                'InvEn':  ('inverter_enabled', '1 if inverter enabled, 0 otherwise'),
                'ChgEn':  ('charger_enabled', '1 if charger enabled, 0 otherwise'),
                'SellEn': ('sell_enabled', '1 if AC1 power generation enabled, 0 otherwise'),
                'OpState':('operating_state', 'device state', {255: 'no data', 5: 'remote power off', 4: 'diagnostic', 3: 'operating', 2: 'standby', 1: 'power save', 0: 'hibernate'}),
                'InvSts': ('inverter_status', 'status of the inverter', { 1024: 'invert', 1025: 'AC passthrough', 1026: 'APS only', 1027: 'load sense', 1028: 'inverter disabled', 1029: 'load sense ready', 1030: 'engaging inverter', 1031: 'invert fault', 1032: 'inverter standby', 1033: 'grid tied', 1034: 'grid support', 1035: 'generator support', 1036: 'sell to grid', 1037: 'load shaving', 1038: 'grid frequency stabilization' }),
                'ChgSts': ('charger_status', 'status of the charger', { 768: 'not charging', 769: 'bulk', 770: 'absorption', 771: 'overcharge', 772: 'equalize', 773: 'float', 774: 'no float', 775: 'constant VI', 776: 'charger disabled', 777: 'qualifying AC', 778: 'qualifying APS', 779: 'engaging charger', 780: 'charge fault', 781: 'charger suspend', 782: 'AC good', 783: 'APS good', 784: 'AC fault', 785: 'charge', 786: 'absorption exit pending', 787: 'ground fault', 788: 'AC good pending' }),
                'ActiveFlt': ('active_faults', 'number of active faults now'),
                'ActiveWrn': ('active_warnings', 'number of active warnings now'),
                'VdcIn':  ('dc_battery_v', 'battery voltage', 1000.0), # divide by 1000
                'IdcIn':  ('dc_battery_a', 'battery current (A)', 1000.0),
                'PdcIn':  ('dc_battery_power_w', 'battery power (W)'),
                'IdcInput': ('dc_inverter_a', 'inverter current from battery (A)', 1000.0),
                'IdcOutput': ('dc_charger_a', 'charger current to battery (A)', 1000.0),
                'PdcInput': ('dc_inverter_power_w', 'inverter power from battery (W)'),
                'PdcOutput': ('dc_charger_power_w', 'charger power to battery (W)'),
                'Tbatt':  ('battery_temp_c', 'battery temp (degrees Celsius)'),
                'ChgModeSts': ('charger_mode', 'unknown state indicator'),
                'VacIn1': ('ac1_v', 'AC1 L1-L2 voltage', 1000.0),
                'IacIn1': ('ac1_a', 'AC1 input current (A)', 1000.0),
                'FacIn1': ('ac1_frequency_hz', 'AC1 frequency (Hz)', 100.0),
                'PacIn1': ('ac1_power_w', 'AC1 power input (W)'),
                'Vac1Ln1': ('ac1_L1_v', 'AC1 L1-N voltage', 1000.0),
                'Iac1Ln1': ('ac1_L1_a', 'AC1 L1-N current (A)', 1000.0),
                'Vac1Ln2': ('ac1_L2_v', 'AC1 L2-N voltage', 1000.0),
                'Iac1Ln2': ('ac1_L2_a', 'AC1 L2-N current (A)', 1000.0),
                'PapparentIn1': ('ac1_var', 'AC1 apparent power input (VAr)'),
                'VacOut1': ('ac1_out_v', 'AC1 output L1-L2 voltage', 1000.0),
                'IacOut1': ('ac1_out_a', 'AC1 output current (A)', 1000.0),
                'FacOut1': ('ac1_out_frequency_hz', 'AC1 output frequency (Hz)', 100.0),
                'PacOut1': ('ac1_out_power_w', 'AC1 output power (W)'),
                'Iac1Net': ('ac1_net_a', 'AC1 net current, generation - consumption (A)', 1000.0),
                'Pac1Net': ('ac1_net_power_w', 'AC1 net power, generation - consumption (W)'),
                'PapparentOut1': ('ac1_out_var', 'AC1 apparent power output (VAr)'),
                'VacIn2': ('ac2_v', 'AC2 L1-L2 voltage', 1000.0),
                'IacIn2': ('ac2_a', 'AC2 input current (A)', 1000.0),
                'FacIn2': ('ac2_frequency_hz', 'AC2 frequency (Hz)', 100.0),
                'PacIn2': ('ac2_power_w', 'AC2 power input (W)'),
                'Vac2Ln1': ('ac2_L1_v', 'AC2 L1-N voltage', 1000.0),
                'Iac2Ln1': ('ac2_L1_a', 'AC2 L1-N current (A)', 1000.0),
                'Vac2Ln2': ('ac2_L2_v', 'AC2 L2-N voltage', 1000.0),
                'Iac2Ln2': ('ac2_L2_a', 'AC2 L2-N current (A)', 1000.0),
                'PapparentGen': ('ac2_var', 'AC2 apparent power input (VAr)'),
                'AcIn1VQual': ('ac1_voltage_qualified', 'AC1 voltage qualification state', qual),
                'AcIn1FQual': ('ac1_frequency_qualified', 'AC1 frequency qualification state', qual),
                'AcIn1TQual': ('ac1_duration_qualified', 'time AC1 has been qualified (sec)'),
                'AcIn2VQual': ('ac2_voltage_qualified', 'AC2 voltage qualification state', qual),
                'AcIn2FQual': ('ac2_frequency_qualified', 'AC2 frequency qualification state', qual),
                'AcIn2TQual': ('ac2_duration_qualified', 'time AC2 has been qualified (sec)'),
                'VacLoad2': ('acout_v', 'ACout L1-L2 voltage', 1000.0),
                'IacLoad2': ('acout_a', 'ACout output current (A)', 1000.0),
                'FacLoad2': ('acout_frequency_hz', 'ACout frequency (Hz)', 100.0),
                'PacLoad2': ('acout_power_w', 'ACout power output (W)'),
                'VacLoad2Ln1': ('acout_L1_v', 'ACout L1-N voltage', 1000.0),
                'IacLoad2Ln1': ('acout_L1_a', 'ACout L1-N current (A)', 1000.0),
                'VacLoad2Ln2': ('acout_L2_v', 'ACout L2-N voltage', 1000.0),
                'IacLoad2Ln2': ('acout_L2_a', 'ACout L2-N current (A)', 1000.0),
                'PapparentLoad2': ('acout_var', 'ACout apparent power output (VAr)'),
                'AuxTrigSts': ('aux_output_mode', 'mode for aux output trigger', { 1: 'Auto On', 2: 'Auto Off', 3: 'Manual On', 4: 'Manual Off' }),
                'AuxOnReason': ('aux_on_reason', 'why aux is on', { 0: 'not on', 1: 'manual on', 2: 'battery voltage low', 3: 'battery voltage high', 4: 'array voltage high', 5: 'battery temp low', 6: 'battery temp high', 7: 'heat sink temp high', 8: 'fault' }),
                'AuxOffReason': ('aux_off_reason', 'why aux is off', { 0: 'not off', 1: 'no active trigger', 2: 'trigger override', 3: 'fault' }),
                'CfgErrors': ('config_errors', 'number of configuration errors'),

                # BATTMON
                'BattV': ('battery_v', 'battery bank voltage', 1000.0),
                'BattI': ('battery_a', 'battery bank charging current', 1000.0),
                'BattT': ('battery_temp_c', 'battery bank temperature (degrees Celsius)'),
                'BattSOC': ('battery_soc_pct', 'battery state of charge (percent full charge)'),
                'BattMidPtV1': ('battery_midpoint1_v', 'battery bank midpoint 1 voltage', 1000.0),
                'BattMidPtV2': ('battery_midpoint2_v', 'battery bank midpoint 2 voltage', 1000.0),
                'BattCapRemaining': ('battery_remaining_ah', 'battery capacity remaining (Ah)'),
                'BattCapRemoved': ('battery_removed_ah', 'battery capacity removed (Ah)'),
                'BattBtsPresent': ('temp_sensor_present', '1 if temp sensor is present, 0 otherwise'),
                'BattTimeToDischarge': ('battery_duration_min', 'time until battery is discharged (min)'),
                'AvgDischg': ('avg_discharge_duration_min', 'avg duration of discharge (min)'),
                'AvgDischgPer': ('avg_discharge_pct', 'avg percentage discharge', 100.0),
                'DeepestDischg': ('deepest_discharge_ah', 'deepest discharge (Ah)'),
                'DeepestDischgPer': ('deepest_discharge_pct', 'deepest percentage discharge', 100.0),
                'CapacityRemoved': ('removed_ah', 'capacity removed (Ah)'),
                'CapacityReturned': ('returned_ah', 'capacity returned (Ah)'),
                'NumChgCycles': ('num_charge_cycles', 'number of charge cycles'),
                'NumSync': ('num_syncs', 'number of synchronizations'),
                'NumDischg': ('num_discharges', 'number of full discharges'),

                # MPPT60
                'VdcIn': ('solar_input_v', 'DC solar input voltage', 1000.0),
                'IdcIn': ('solar_input_a', 'solar input current (A)', 1000.0),
                'PdcIn': ('solar_input_power_w', 'solar input power (W)'),
                'VdcOut': ('charge_output_v', 'DC battery charge voltage', 1000.0),
                'IdcOut': ('charge_output_a', 'battery charge current (A)', 1000.0),
                'PdcOut': ('charge_output_power_w', 'battery charge power (W)'),
                'Tbatt': ('battery_temp_c', 'battery temp (degrees Celsius)'),
                'Vaux':  ('aux_v', 'auxiliary trigger voltage', 1000.0),

                # XW AGS
                'GenState': ('generator_state', 'state of generator controller', { 0: 'quiet time', 1: 'auto on', 2: 'auto off', 3: 'manual on', 4: 'manual off', 5: 'gen shutdown', 6: 'external shutdown', 7: 'AGS fault', 8: 'suspend', 9: 'not operating' }),
                'GenAction': ('generator_action', 'generator action being called for', { 0: 'preheating', 1: 'start delay', 2: 'cranking', 3: 'starter cooling', 4: 'warming up', 5: 'cooling down', 6: 'spinning down', 7: 'shutdown bypass', 8: 'stopping', 9: 'running', 10: 'stopped', 11: 'crank delay' }),
                'GenOnReason': ('gen_on_reason', 'why generator is on', { 0: 'not on', 1: 'DC voltage low', 2: 'battery SOC low', 3: 'AC current high', 4: 'contact closed', 5: 'manual on', 6: 'exercise', 7: 'non quiet time', 8: 'external on via AGS', 9: 'external on via generator', 10: 'unable to stop', 11: 'AC power high', 12: 'DC current high' }),
                'GenOffReason': ('gen_off_reason', 'why generator is off', { 0: 'not off', 1: 'DC voltage high', 2: 'battery SOC high', 3: 'AC current low', 4: 'contact opened', 5: 'reached absorption phase', 6: 'reached float phase', 7: 'manual off', 8: 'max run time', 9: 'max auto cycle', 10: 'exercise done', 11: 'quiet time', 12: 'external off via AGS', 13: 'safe mode', 14: 'external off via generator', 15: 'external shutdown', 16: 'auto off', 17: 'fault', 18: 'unable to start', 19: 'power low', 20: 'DC current low', 21: 'AC good' }),
            }
            ignore = {
                'DeviceName', 'FGANumber', 'UniqueIDNumber', 'SerialNumber', 'FirmwareVersion',
                'MBAddr', 'DcSrcID',
            }
            for (k, v) in d.items():
                if k in ignore:
                    continue
                if k == 'BattTimeToDischarge':
                    # 240 Hr 00
                    (hours, sep, minutes) = v.partition(' Hr ')
                    v = int(hours) * 60 + int(minutes)
                try:
                    v = float(v)
                except ValueError:
                    pass
                if k == 'Tbatt' or k == 'BattT':
                    if v == 65535:
                        continue # temperature sensor not connected
                    v = (v / 100.0) - 273 # Kelvins to degrees Celsius
                lup = xlate.get(k)
                if lup is None:
                    LOGGER.info(f'no lookup table entry for {k} with value {v}, {infovalues}')
                    continue
                (metric, desc, *rest) = lup
                scale = 1
                anotherlabel, anothervalue = [], []
                if rest:
                    assert len(rest) == 1, rest
                    arg = rest[0]
                    if type(arg) != type({}):
                        scale = arg
                    else:
                        state = arg.get(v)
                        if state is None:
                            LOGGER.info(f'no state entry for value {v}, key {k}, {arg}')
                        else:
                            anotherlabel, anothervalue = ['state'], [state]
                g = makegauge(metric, desc, morelabels=anotherlabel)
                g.add_metric(labelvalues + anothervalue, v/scale)

        return metric_to_gauge.values()
    
class ComboxWeb:
    def __init__(self, uri, timeout=20, verify=None):
        if uri.endswith('/'):
            uri = uri[:len(uri)-1]
        if uri.find('://') == -1:
            uri = f'http://{uri}'
        self.uri, self.timeout, self.verify = uri, timeout, verify
        self.reconnect()
    
    def reconnect(self):
        self.session = requests.Session()
        (scheme, punct, domain) = self.uri.partition('://')
        for name in ['Warning_ACKed_2', 'Warning_ACKed_3']:
            saw_warning_cookie = requests.cookies.create_cookie(domain=domain,name=name,value='1')
            self.session.cookies.set_cookie(saw_warning_cookie)
        self.session.verify = self.verify
    
    def _set_headers(self):
        self.session.headers.update({
            'Origin': self.uri,
        })
    
    def login(self, user, password):
        LOGIN_ATTEMPTS.inc()
        self.user, self.password = user, password
        authinfo = {
            'login_username': user,
            'login_password': password,
            'submit': 'Log In',
        }
        self._set_headers()
        p = self.session.post('%s/login.cgi' % self.uri, data=authinfo, timeout=self.timeout)
        p.raise_for_status()
        self._write('0duringlogin', p)
        LOGGER.info(f'authenticated to {self.uri} as {user}')
  
        # we seem not to need this
        # self._set_headers()
        # p = self.session.get('%s/gethandler.json?name=ip' % self.uri)
        # p = self.session.get('%s/gethandler.json?name=WEBPORTAL.ENABLE' % self.uri)
        # p = self.session.post('%s/posthandler.cgi' % self.uri, data={'WEBPORTAL.ENABLE':1})
        # p = self.session.post('%s/posthandler.cgi' % self.uri, data={'EXEC': 'commit'})
        # p = self.session.get('%s/gethandler.json?name=exec' % self.uri)
  
    # to get a name to pass to get_deviceinfo(), use
    #    '%s(%s)' % (listitem['family'], listitem['UniqueID'])
    def get_devicelist(self, name='XBGATEWAY.DEVLIST'):
        return self._get_json(name)
    
    def get_deviceinfo(self, device):
        """device is one of the elements returned by get_devicelist()"""
        # name is e.g. AGS(1389958) or XW(1511331)
        name = f"{device['family']}({device['UniqueID']})"
        return self._get_json('%s.INFO' % name)
    
    def get_variablelist(self, name='XBGATEWAY.VARLIST'):
        return self._get_vareqval(name)
  
    def get_sysvars(self):
        """Not sure what these are good for if anything."""
        self._set_headers()
        # also xbsysvars.jgz which is different
        sysvars = self.session.get('%s/meta/sysvars.jgz' % self.uri, timeout=self.timeout)
        sysvars.raise_for_status()
        return sysvars.json()
    
    def _dereference_and_clean(self, response, name):
        r = response.text
        return response.json()['values'].get(name, '').replace('&#0D;&#0A;', '\n').replace('&#22;', '"').replace('&#09;', ' ') if r else ''
  
    # if you're not logged in or your login has expired, this will raise
    # json.decoder.JSONDecodeError because the server responds with HTTP status 200
    # and an empty body, which the JSON decoder won't accept.
    # TODO: detect this and raise our own NotLoggedInError.
  
    def _get_json(self, name):
        uri = '%s/gethandler.json?name=%s' % (self.uri, name)
        self._set_headers()
        r = self.session.get(uri, timeout=self.timeout)
        r.raise_for_status()
        self._write('rjson', r)
        try:
            return json.loads(self._dereference_and_clean(r, name))
        except JSONDecodeError:
            if name.startswith('SCP2'):
                return {}  # is usually empty so don't retry
            LOGGER.info(f'reconnecting due to JSON error loading {name}')
            self.reconnect()
            self.login(self.user, self.password)
            self._set_headers()
            r2 = self.session.get(uri, timeout=self.timeout)
            r2.raise_for_status()
            # if this fails again, we won't try to catch it
            js = self._dereference_and_clean(r2, name)
            return json.loads(js) if js else {}
    
    def _get_vareqval(self, name):
        uri = '%s/gethandler.json?name=%s' % (self.uri, name)
        self._set_headers()
        r = self.session.get(uri, timeout=self.timeout)
        r.raise_for_status()
        self._write('rvareqval', r)
        ret = []
        for line in self._dereference_and_clean(r, name).split('\n'):
            st = line.strip()
            if st:
                (var, eq, val) = st.partition('=')
                ret.append((var, val))
        return ret
  
    def close(self):
        self.session.close()
  
    def _write(self, basename, r):
        """during debugging, this method writes request/response info"""
        return # not debugging now :)
        with open('DEBUG-%s.html' % basename, 'w') as f:
            f.write(r.text)
        with open('DEBUG-%s.requests' % basename, 'w') as f:
            for h in r.history:
                f.write('request %s %s\nresponse %d %s\n\n' % (h.url, h.request.headers, h.status_code, h.headers))
            f.write('request %s %s\nresponse %d %s\n\n' % (r.url, r.request.headers, r.status_code, r.headers)) # dict(r.cookies)

  
if __name__ == "__main__":
    import sys, yaml
    assert len(sys.argv) == 3, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    c = ComboxClient(config)
    client = ComboxWeb(sys.argv[2], timeout=config['combox']['timeout'])
    client.login(config['combox']['user'], config['combox']['password'])

    for d in client.get_devicelist():
        info = client.get_deviceinfo(d)
        print(json.dumps(info, indent=2))

    for v in client.get_variablelist():
        print(v)
