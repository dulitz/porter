"""
test_rinnai.py

Currently we only test the mutation operations: run() etc.

TODO: Test more operations.
"""

import asyncio
import rinnai

config = {
    'rinnai': {
        'credentials': {
            'test@googlemail.com': 'password'
        }
    }
}

class MockWaterHeater:
    def __init__(self):
        self.state = 0
    def start_recirculation(self, device, minutes):
        assert self.state == 0, self.state
        assert device['device_name'] == 'myhouse', device
        assert minutes == 5, minutes
        self.state += 1

class MockRinnaiClient(rinnai.RinnaiClient):
    def get_devices(self, target):
        assert target == 'test@googlemail.com', target
        c = MockWaterHeater()
        self.emailtoclient[target] = c
        return [
            {'id': 'yes', 'thing_name': 'wazoo', 'device_name': 'myhouse', 'dealer_uuid': None, 'city': None, 'state': None, 'street': None, 'zip': None, 'country': None, 'firmware': '232', 'model': None, 'dsn': 'woot', 'user_uuid': 'ohyeahhh', 'connected_at': None, 'key': None, 'lat': None, 'lng': None, 'address': None, 'vacation': False, 'createdAt': '2021-06-16T23:46:50.597Z', 'updatedAt': '2021-06-16T23:49:11.132Z', 'activity': {'clientId': '69', 'serial_id': '55', 'timestamp': '1625526436397', 'eventType': 'connected'}, 'shadow': {'heater_serial_number': '2', 'ayla_dsn': None, 'rinnai_registered': None, 'do_maintenance_retrieval': True, 'model': None, 'module_log_level': None, 'set_priority_status': True, 'set_recirculation_enable': None, 'set_recirculation_enabled': True, 'set_domestic_temperature': '125', 'set_operation_enabled': None, 'schedule': '017001', 'schedule_holiday': None, 'schedule_enabled': True, 'do_zigbee': None, 'timezone': 'PST8PDT,M3.2.0,M11.1.0', 'timezone_encoded': None, 'priority_status': True, 'recirculation_enabled': False, 'recirculation_duration': '5', 'lock_enabled': False, 'operation_enabled': True, 'module_firmware_version': '232', 'recirculation_not_configured': None, 'maximum_domestic_temperature': None, 'minimum_domestic_temperature': None, 'createdAt': '2021-06-16T23:49:11.056Z', 'updatedAt': '2021-07-06T00:45:01.749Z'}, 'monitoring': None, 'schedule': {'items': [{'id': 'hoo', 'serial_id': '2', 'name': 'evening', 'schedule': None, 'days': ['{0=Su, 1=M, 2=T, 3=W, 4=Th, 5=F, 6=S}'], 'times': ['{start=5:30 pm, end=5:45 pm}'], 'schedule_date': '2021-06-16T18:39:54-07:00', 'active': True, 'createdAt': '2021-06-17T01:39:55.873Z', 'updatedAt': '2021-06-17T01:39:55.873Z'}, {'id': '62ab', 'serial_id': '5', 'name': 'Daily', 'schedule': None, 'days': ['{0=Su, 1=M, 2=T, 3=W, 4=Th, 5=F, 6=S}'], 'times': ['{start=5:45 am, end=6:00 am}'], 'schedule_date': '2021-06-16T16:54:10-07:00', 'active': True, 'createdAt': '2021-06-16T23:54:11.670Z', 'updatedAt': '2021-06-16T23:54:11.670Z'}, {'id': '71', 'serial_id': '5', 'name': 'second morning', 'schedule': None, 'days': ['{0=Su, 1=M, 2=T, 3=W, 4=Th, 5=F, 6=S}'], 'times': ['{start=7:00 am, end=7:15 am}'], 'schedule_date': '2021-06-16T18:39:26-07:00', 'active': True, 'createdAt': '2021-06-17T01:39:27.843Z', 'updatedAt': '2021-06-17T01:39:27.843Z'}], 'nextToken': None}, 'info': {'serial_id': '5', 'ayla_dsn': None, 'name': '9', 'domestic_combustion': 'false', 'domestic_temperature': '125', 'wifi_ssid': 'H', 'wifi_signal_strength': '-74', 'wifi_channel_frequency': '2462', 'local_ip': '1.1.1.1', 'public_ip': '1.7.1.2', 'ap_mac_addr': '1', 'recirculation_temperature': None, 'recirculation_duration': '5', 'zigbee_inventory': '[]', 'zigbee_status': None, 'lime_scale_error': None, 'mc__total_calories': None, 'type': 'info', 'unix_time': '1625547220', 'm01_water_flow_rate_raw': '0', 'do_maintenance_retrieval': None, 'aft_tml': None, 'tot_cli': None, 'unt_mmp': None, 'aft_tmh': None, 'bod_tmp': None, 'm09_fan_current': '0', 'm02_outlet_temperature': '103', 'firmware_version': None, 'bur_thm': None, 'tot_clm': None, 'exh_tmp': None, 'm05_fan_frequency': '0', 'thermal_fuse_temperature': None, 'm04_combustion_cycles': '7', 'hardware_version': None, 'm11_heat_exchanger_outlet_temperature': '121', 'bur_tmp': None, 'tot_wrl': None, 'm12_bypass_servo_position': '37', 'm08_inlet_temperature': '99', 'm20_pump_cycles': '0', 'module_firmware_version': None, 'error_code': '   ', 'warning_code': '   ', 'internal_temperature': None, 'tot_wrm': None, 'unknown_b': None, 'rem_idn': None, 'm07_water_flow_control_position': '1', 'operation_hours': None, 'thermocouple': None, 'tot_wrh': None, 'recirculation_capable': 'true', 'maintenance_list': '1,2,3,4,5,6,7,8,9,10,11,12,15,19,20,21,100,101,102,120,121,122', 'tot_clh': None, 'temperature_table': '2', 'm19_pump_hours': '0', 'oem_host_version': None, 'schedule_a_name': None, 'zigbee_pairing_count': None, 'schedule_c_name': None, 'schedule_b_name': None, 'model': None, 'schedule_d_name': None, 'total_bath_fill_volume': None, 'dt': None, 'createdAt': '2021-07-06T04:53:41.179Z', 'updatedAt': '2021-07-06T04:53:41.179Z'}, 'errorLogs': {'items': [], 'nextToken': None}, 'registration': {'items': [], 'nextToken': None}}
            ]
    def verify(self):
        s = self.emailtoclient['test@googlemail.com'].state
        assert s == 1, s

client = MockRinnaiClient(config)

asyncio.run(client.run('test@googlemail.com', 'myhouse', 'start_recirculation', 5))
client.verify()

print('success')
