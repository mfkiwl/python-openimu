import os
import struct
import time
import json
import datetime
import threading
import math
import re
import struct
from ..widgets import (
    NTRIPClient, EthernetDataLogger, EthernetDebugDataLogger, EthernetRTCMDataLogger
)
from ...framework.utils import (
    helper, resource
)
from ...framework.context import APP_CONTEXT
from ...framework.utils.firmware_parser import parser as firmware_content_parser
from ..base.provider_base import OpenDeviceBase
from ..configs.openrtk_predefine import (
    APP_STR, get_openrtk_products, get_configuratin_file_mapping
)
from ..decorator import with_device_message
from ...models import InternalCombineAppParseRule
from ..parsers.open_field_parser import encode_value
from ...framework.utils.print import print_yellow
from ..upgrade_workers import (
    EthernetFirmwareUpgradeWorker,
    FirmwareUpgradeWorker,
    SDK9100UpgradeWorker,
    JumpBootloaderWorker,
    JumpApplicationWorker,
    UPGRADE_EVENT,
    UPGRADE_GROUP
)


class Provider(OpenDeviceBase):
    '''
    INS401 Ethernet 100base-t1 provider
    '''

    def __init__(self, communicator, *args):
        super(Provider, self).__init__(communicator)
        self.type = 'INS401'
        self.server_update_rate = 100
        self.sky_data = []
        self.pS_data = []
        self.app_config_folder = ''
        self.device_info = None
        self.app_info = None
        self.parameters = None
        self.setting_folder_path = None
        self.data_folder = None
        self.debug_serial_port = None
        self.rtcm_serial_port = None
        self.user_logf = None
        self.debug_logf = None
        self.rtcm_logf = None
        self.debug_c_f = None
        self.enable_data_log = False
        self.is_app_matched = False
        self.ntrip_client_enable = False
        self.nmea_buffer = []
        self.nmea_sync = 0
        self.prepare_folders()
        self.ntrip_client = None
        self.connected = True
        self.rtk_log_file_name = ''

    def prepare_folders(self):
        '''
        Prepare folders for data storage and configuration
        '''
        executor_path = resource.get_executor_path()
        setting_folder_name = 'setting'

        data_folder_path = os.path.join(executor_path, 'data')
        if not os.path.isdir(data_folder_path):
            os.makedirs(data_folder_path)
        self.data_folder = data_folder_path

        # copy contents of app_config under executor path
        self.setting_folder_path = os.path.join(
            executor_path, setting_folder_name)

        all_products = get_openrtk_products()
        config_file_mapping = get_configuratin_file_mapping()

        for product in all_products:
            product_folder = os.path.join(self.setting_folder_path, product)
            if not os.path.isdir(product_folder):
                os.makedirs(product_folder)

            for app_name in all_products[product]:
                app_name_path = os.path.join(product_folder, app_name)
                app_name_config_path = os.path.join(
                    app_name_path, config_file_mapping[product])

                if not os.path.isfile(app_name_config_path):
                    if not os.path.isdir(app_name_path):
                        os.makedirs(app_name_path)
                    app_config_content = resource.get_content_from_bundle(
                        setting_folder_name, os.path.join(product, app_name, config_file_mapping[product]))
                    if app_config_content is None:
                        continue

                    with open(app_name_config_path, "wb") as code:
                        code.write(app_config_content)

    @property
    def is_in_bootloader(self):
        ''' Check if the connected device is in bootloader mode
        '''
        if not self.app_info or not self.app_info.__contains__('version'):
            return False

        version = self.app_info['version']
        version_splits = version.split(',')
        if len(version_splits) == 1:
            if 'bootloader' in version_splits[0].lower():
                return True

        return False

    def bind_device_info(self, device_access, device_info, app_info):
        self._build_device_info(device_info)
        self._build_app_info(app_info)
        self.connected = True

        self._device_info_string = '# Connected {0} with ethernet #\n\rDevice: {1} \n\rFirmware: {2}'\
            .format('INS401', device_info, app_info)

        return self._device_info_string

    def _build_device_info(self, text):
        '''
        Build device info
        '''
        split_text = text.split(' ')

        self.device_info = {
            'name': split_text[0],
            'pn': split_text[1],
            'sn': split_text[2]
        }

    def _build_app_info(self, text):
        '''
        Build app info
        '''

        app_version = text

        split_text = app_version.split(' ')
        app_name = next(
            (item for item in APP_STR if item in split_text), None)

        if not app_name:
            app_name = 'RTK_INS'
            self.is_app_matched = False
        else:
            self.is_app_matched = True

        self.app_info = {
            'app_name': app_name,
            'app_version': split_text[1] + split_text[2],
            'bootloader_version': split_text[3] + split_text[4],
        }

    def load_properties(self):
        # Load config from user working path
        local_config_file_path = os.path.join(os.getcwd(), 'ins401.json')
        if os.path.isfile(local_config_file_path):
            with open(local_config_file_path) as json_data:
                self.properties = json.load(json_data)
                return

        # Load the openimu.json based on its app
        product_name = self.device_info['name']
        app_name = 'RTK_INS'  # self.app_info['app_name']
        app_file_path = os.path.join(
            self.setting_folder_path, product_name, app_name, 'ins401.json')

        with open(app_file_path) as json_data:
            self.properties = json.load(json_data)

        if not self.is_app_matched:
            print_yellow(
                'Failed to extract app version information from unit.' +
                '\nThe supported application list is {0}.'.format(APP_STR) +
                '\nTo keep runing, use INS configuration as default.' +
                '\nYou can choose to place your json file under execution path if it is an unknown application.')

    def ntrip_client_thread(self):
        self.ntrip_client = NTRIPClient(self.properties)
        self.ntrip_client.on('parsed', self.handle_rtcm_data_parsed)
        if self.device_info.__contains__('sn') and self.device_info.__contains__('pn'):
            self.ntrip_client.set_connect_headers({
                'Ntrip-Sn': self.device_info['sn'],
                'Ntrip-Pn': self.device_info['pn']
            })
        self.ntrip_client.run()

    def handle_rtcm_data_parsed(self, data):
        # print('rtcm',data)

        if self.rtcm_logf is not None and data is not None:
            self.rtcm_logf.write(bytes(data))
            self.rtcm_logf.flush()

        if self.communicator.can_write() and not self.is_upgrading:
            whole_packet = helper.build_ethernet_packet(
                self.communicator.get_dst_mac(),
                self.communicator.get_src_mac(),
                b'\x02\x0b',
                data)

            self.communicator.write(whole_packet)
            pass

    def after_setup(self):
        set_user_para = self.cli_options and self.cli_options.set_user_para
        self.ntrip_client_enable = self.cli_options and self.cli_options.ntrip_client
        # with_raw_log = self.cli_options and self.cli_options.with_raw_log

        if set_user_para:
            result = self.set_params(
                self.properties["initial"]["userParameters"])
            ##print('set user para {0}'.format(result))
            if result['packetType'] == 'success':
                self.save_config()

        # start ntrip client
        if self.properties["initial"].__contains__("ntrip") and not self.ntrip_client and not self.is_in_bootloader:
            threading.Thread(target=self.ntrip_client_thread).start()

        try:
            if self.data_folder is not None:
                dir_time = time.strftime("%Y%m%d_%H%M%S", time.localtime())
                file_time = time.strftime(
                    "%Y_%m_%d_%H_%M_%S", time.localtime())
                file_name = self.data_folder + '/' + 'ins401_log_' + dir_time
                os.mkdir(file_name)
                self.rtk_log_file_name = file_name
                self.user_logf = open(
                    file_name + '/' + 'user_' + file_time + '.bin', "wb")
                # self.debug_logf = open(
                #     file_name + '/' + 'debug_' + file_time + '.bin', "wb")
                self.rtcm_logf = open(
                    file_name + '/' + 'rtcm_base_' + file_time + '.bin', "wb")
                self.rtcm_rover_logf = open(
                    file_name + '/' + 'rtcm_rover_' + file_time + '.bin', "wb")

            # start a thread to log data
            # threading.Thread(target=self.thread_data_log).start()
            # threading.Thread(target=self.thread_debug_data_log).start()
            # threading.Thread(target=self.thread_rtcm_data_log).start()

            self.save_device_info()
        except Exception as e:
            print(e)
            return False

    def nmea_checksum(self, data):
        data = data.replace("\r", "").replace("\n", "").replace("$", "")
        nmeadata, cksum = re.split('\*', data)
        calc_cksum = 0
        for s in nmeadata:
            calc_cksum ^= ord(s)
        return int(cksum, 16), calc_cksum

    def on_read_raw(self, data):
        for bytedata in data:
            if bytedata == 0x24:
                self.nmea_buffer = []
                self.nmea_sync = 0
                self.nmea_buffer.append(chr(bytedata))
            else:
                self.nmea_buffer.append(chr(bytedata))
                if self.nmea_sync == 0:
                    if bytedata == 0x0D:
                        self.nmea_sync = 1
                elif self.nmea_sync == 1:
                    if bytedata == 0x0A:
                        try:
                            str_nmea = ''.join(self.nmea_buffer)
                            cksum, calc_cksum = self.nmea_checksum(
                                str_nmea)
                            if cksum == calc_cksum:
                                if str_nmea.find("$GPGGA") != -1:
                                    if self.ntrip_client:
                                        self.ntrip_client.send(str_nmea)
                                self.user_logf.write(str_nmea.encode())
                            APP_CONTEXT.get_print_logger().info(str_nmea.replace('\r\n', ''))
                        except Exception as e:
                            # print('NMEA fault:{0}'.format(e))
                            pass
                    self.nmea_buffer = []
                    self.nmea_sync = 0

        # if self.user_logf is not None and data is not None:
        #     self.user_logf.write(data)
        #     self.user_logf.flush()

    def thread_data_log(self, *args, **kwargs):
        self.ethernet_data_logger = EthernetDataLogger(
            self.properties, self.communicator, self.user_logf)
        self.ethernet_data_logger.run()

    def thread_debug_data_log(self, *args, **kwargs):
        self.ethernet_debug_data_logger = EthernetDebugDataLogger(
            self.properties, self.communicator, self.debug_logf)
        self.ethernet_debug_data_logger.run()

    def thread_rtcm_data_log(self, *args, **kwargs):
        self.ethernet_rtcm_data_logger = EthernetRTCMDataLogger(
            self.properties, self.communicator, self.rtcm_logf)
        self.ethernet_rtcm_data_logger.run()

    def on_receive_output_packet(self, packet_type, data, error=None):
        '''
        Listener for getting output packet
        '''
        if packet_type == b'\x06\n':
            if self.rtcm_rover_logf:
                self.rtcm_rover_logf.write(bytes(data))
        else:
            if self.user_logf:
                self.user_logf.write(bytes(data))

    def before_write_content(self, core, content_len):
        command_CS = [0x04, 0xaa]

        message_bytes = [ord('C'), ord(core)]
        message_bytes.extend(struct.pack('>I', content_len))

        command_line = helper.build_ethernet_packet(
            self.communicator.get_dst_mac(),
            self.communicator.get_src_mac(),
            command_CS, message_bytes)

        time.sleep(3)  # sleep 3s, to wait for bootloader ready

        command_filter = struct.unpack('>H', bytes(command_CS))[0]
        result = self.communicator.write_read(command_line, command_filter)

        if not result:
            raise Exception('Cannot run set core command')

    def build_worker(self, rule, content):
        ''' Build upgarde worker by rule and content
        '''
        if rule == 'rtk':
            rtk_upgrade_worker = FirmwareUpgradeWorker(
                self.communicator, lambda: helper.format_firmware_content(content), 192)
            rtk_upgrade_worker.name = 'MAIN_RTK'
            rtk_upgrade_worker.on(
                UPGRADE_EVENT.FIRST_PACKET, lambda: time.sleep(12))
            rtk_upgrade_worker.on(UPGRADE_EVENT.BEFORE_WRITE,
                                  lambda: self.before_write_content('0', len(content)))
            return rtk_upgrade_worker

        if rule == 'ins':
            ins_upgrade_worker = FirmwareUpgradeWorker(
                self.communicator, lambda: helper.format_firmware_content(content), 192)
            ins_upgrade_worker.name = 'MAIN_RTK'
            ins_upgrade_worker.group = UPGRADE_GROUP.FIRMWARE
            ins_upgrade_worker.on(
                UPGRADE_EVENT.FIRST_PACKET, lambda: time.sleep(12))
            ins_upgrade_worker.on(UPGRADE_EVENT.BEFORE_WRITE,
                                  lambda: self.before_write_content('1', len(content)))
            return ins_upgrade_worker

        if rule == 'sdk':
            sdk_upgrade_worker = SDK9100UpgradeWorker(
                self.communicator, lambda: helper.format_firmware_content(content), 192)
            sdk_upgrade_worker.group = UPGRADE_GROUP.FIRMWARE

        if rule == 'imu':
            imu_upgrade_worker = FirmwareUpgradeWorker(
                self.communicator, lambda: helper.format_firmware_content(content), 192)
            imu_upgrade_worker.name = 'SUB_IMU'
            imu_upgrade_worker.group = UPGRADE_GROUP.FIRMWARE
            imu_upgrade_worker.on(
                UPGRADE_EVENT.FIRST_PACKET, lambda: time.sleep(5))
            imu_upgrade_worker.on(UPGRADE_EVENT.BEFORE_WRITE,
                                  lambda: self.before_write_content('0', len(content)))
            return imu_upgrade_worker

    def get_upgrade_workers(self, firmware_content):
        workers = []
        rules = [
            InternalCombineAppParseRule('rtk', 'rtk_start:', 4),
            InternalCombineAppParseRule('ins', 'ins_start:', 4),
            InternalCombineAppParseRule('sdk', 'sdk_start:', 4),
            InternalCombineAppParseRule('imu', 'imu_start:', 4),
        ]

        parsed_content = firmware_content_parser(firmware_content, rules)

        # foreach parsed content, if empty, skip register into upgrade center
        for _, rule in enumerate(parsed_content):
            content = parsed_content[rule]
            if len(content) == 0:
                continue

            worker = self.build_worker(rule, content)
            if not worker:
                continue

            workers.append(worker)

        # wrap ins bootloader
        start_index = -1
        end_index = -1
        for i, worker in enumerate(workers):
            if isinstance(worker, FirmwareUpgradeWorker) and worker.name == 'MAIN_RTK':
                start_index = i if start_index == -1 else start_index
                end_index = i
        dst_mac = self.communicator.get_dst_mac()
        src_mac = self.communicator.get_src_mac()
        ins_jump_bootloader_command = helper.build_ethernet_packet(
            dst_mac, src_mac, bytes([0x01, 0xaa]))
        ins_jump_bootloader_worker = JumpBootloaderWorker(
            self.communicator, command=ins_jump_bootloader_command)
        ins_jump_bootloader_worker.group = UPGRADE_GROUP.FIRMWARE

        ins_jump_application_command = helper.build_ethernet_packet(
            dst_mac, src_mac, bytes([0x02, 0xaa]))
        ins_jump_application_worker = JumpApplicationWorker(
            self.communicator, command=ins_jump_application_command)
        ins_jump_application_worker.group = UPGRADE_GROUP.FIRMWARE

        if start_index > -1 and end_index > -1:
            workers.insert(
                start_index, ins_jump_bootloader_worker)
            workers.insert(
                end_index+2, ins_jump_application_worker)

        # wrap imu bootloader
        start_index = -1
        end_index = -1
        for i, worker in enumerate(workers):
            if isinstance(worker, FirmwareUpgradeWorker) and worker.name == 'SUB_IMU':
                start_index = i if start_index == -1 else start_index
                end_index = i

        imu_jump_bootloader_command = helper.build_ethernet_packet(
            dst_mac, src_mac, bytes([0x49, 0x4a]), False)
        imu_jump_bootloader_worker = JumpBootloaderWorker(
            self.communicator, command=imu_jump_bootloader_command)
        imu_jump_bootloader_worker.group = UPGRADE_GROUP.FIRMWARE

        imu_jump_application_command = helper.build_ethernet_packet(
            dst_mac, src_mac, bytes([0x41, 0x4a]), False)
        imu_jump_application_worker = JumpApplicationWorker(
            self.communicator, command=imu_jump_application_command)
        imu_jump_application_worker.group = UPGRADE_GROUP.FIRMWARE

        if start_index > -1 and end_index > -1:
            workers.insert(
                start_index, imu_jump_bootloader_worker)
            workers.insert(
                end_index+2, imu_jump_application_worker)

        return workers

    def get_device_connection_info(self):
        return {
            'modelName': self.device_info['name'],
            'deviceType': self.type,
            'serialNumber': self.device_info['sn'],
            'partNumber': self.device_info['pn'],
            'firmware': self.device_info['firmware_version']
        }

    def get_operation_status(self):
        if self.is_logging:
            return 'LOGGING'

        return 'IDLE'

    def save_device_info(self):
        if not self.rtk_log_file_name or not self._device_info_string:
            return

        local_time = time.localtime()
        formatted_file_time = time.strftime("%Y_%m_%d_%H_%M_%S", local_time)
        file_path = os.path.join(
            self.rtk_log_file_name,
            'device_info_{0}.txt'.format(formatted_file_time)
        )
        with open(file_path, 'w') as outfile:
            outfile.write(self._device_info_string)

    def after_upgrade_completed(self):
        # start ntrip client
        if self.properties["initial"].__contains__("ntrip") and not self.ntrip_client and not self.is_in_bootloader:
            thead = threading.Thread(target=self.ntrip_client_thread)
            thead.start()

        self.save_device_info()

    # command list
    def server_status(self, *args):  # pylint: disable=invalid-name
        '''
        Get server connection status
        '''
        return {
            'packetType': 'ping',
            'data': {'status': '1'}
        }

    def get_device_info(self, *args):  # pylint: disable=invalid-name
        '''
        Get device information
        '''
        return {
            'packetType': 'deviceInfo',
            'data':  [
                {'name': 'Product Name', 'value': self.device_info['name']},
                {'name': 'IMU', 'value': self.device_info['imu']},
                {'name': 'PN', 'value': self.device_info['pn']},
                {'name': 'Firmware Version',
                 'value': self.device_info['firmware_version']},
                {'name': 'SN', 'value': self.device_info['sn']},
                {'name': 'App Version', 'value': self.app_info['version']}
            ]
        }

    def get_log_info(self):
        '''
        Build information for log
        '''
        return {
            "type": self.type,
            "model": self.device_info['name'],
            "logInfo": {
                "pn": self.device_info['pn'],
                "sn": self.device_info['sn'],
                "rtkProperties": json.dumps(self.properties)
            }
        }

    def get_conf(self, *args):  # pylint: disable=unused-argument
        '''
        Get json configuration
        '''
        return {
            'packetType': 'conf',
            'data': {
                'outputs': self.properties['userMessages']['outputPackets'],
                'inputParams': self.properties['userConfiguration']
            }
        }

    @with_device_message
    def get_params(self, *args):  # pylint: disable=unused-argument
        '''
        Get all parameters
        '''
        has_error = False
        parameter_values = []

        if self.app_info['app_name'] == 'INS':
            conf_parameters = self.properties['userConfiguration']
            conf_parameters_len = len(conf_parameters)-1
            step = 10

            for i in range(2, conf_parameters_len, step):
                start_byte = i
                end_byte = i+step-1 if i+step < conf_parameters_len else conf_parameters_len

                command_line = helper.build_packet(
                    'gB', [start_byte, end_byte])
                result = yield self._message_center.build(command=command_line, timeout=2)
                if result['error']:
                    has_error = True
                    break

                parameter_values.extend(result['data'])
        else:
            command_line = helper.build_input_packet('gA')
            result = yield self._message_center.build(command=command_line, timeout=3)
            if result['error']:
                has_error = True

            parameter_values = result['data']

        if not has_error:
            self.parameters = parameter_values
            yield {
                'packetType': 'inputParams',
                'data': parameter_values
            }

        yield {
            'packetType': 'error',
            'data': 'No Response'
        }

    @with_device_message
    def get_param(self, params, *args):  # pylint: disable=unused-argument
        '''
        Update paramter value
        '''
        command_line = helper.build_input_packet(
            'gP', properties=self.properties, param=params['paramId'])
        # self.communicator.write(command_line)
        # result = self.get_input_result('gP', timeout=1)
        result = yield self._message_center.build(command=command_line)

        data = result['data']
        error = result['error']

        if error:
            yield {
                'packetType': 'error',
                'data': 'No Response'
            }

        if data:
            self.parameters = data
            yield {
                'packetType': 'inputParam',
                'data': data
            }

        yield {
            'packetType': 'error',
            'data': 'No Response'
        }

    @with_device_message
    def set_params(self, params, *args):  # pylint: disable=unused-argument
        '''
        Update paramters value
        '''
        input_parameters = self.properties['userConfiguration']
        grouped_parameters = {}

        for parameter in params:
            exist_parameter = next(
                (x for x in input_parameters if x['paramId'] == parameter['paramId']), None)

            if exist_parameter:
                has_group = grouped_parameters.__contains__(
                    exist_parameter['category'])
                if not has_group:
                    grouped_parameters[exist_parameter['category']] = []

                current_group = grouped_parameters[exist_parameter['category']]

                current_group.append(
                    {'paramId': parameter['paramId'], 'value': parameter['value'], 'type': exist_parameter['type']})

        for group in grouped_parameters.values():
            message_bytes = []
            for parameter in group:
                message_bytes.extend(
                    encode_value('int8', parameter['paramId'])
                )
                message_bytes.extend(
                    encode_value(parameter['type'], parameter['value'])
                )
                # print('parameter type {0}, value {1}'.format(
                #     parameter['type'], parameter['value']))
            # result = self.set_param(parameter)
            command_line = helper.build_packet(
                b'\x03\xcc', message_bytes)
            # for s in command_line:
            #     print(hex(s))

            result = yield self._message_center.build(command=command_line)

            packet_type = result['packet_type']
            data = result['data']

            if packet_type == 'error':
                yield {
                    'packetType': 'error',
                    'data': {
                        'error': data
                    }
                }
                break

            if data > 0:
                yield {
                    'packetType': 'error',
                    'data': {
                        'error': data
                    }
                }
                break

        yield {
            'packetType': 'success',
            'data': {
                'error': 0
            }
        }

    @with_device_message
    def set_param(self, params, *args):  # pylint: disable=unused-argument
        '''
        Update paramter value
        '''
        command_line = helper.build_input_packet(
            'uP', properties=self.properties, param=params['paramId'], value=params['value'])
        # self.communicator.write(command_line)
        # result = self.get_input_result('uP', timeout=1)
        result = yield self._message_center.build(command=command_line)

        error = result['error']
        data = result['data']
        if error:
            yield {
                'packetType': 'error',
                'data': {
                    'error': data
                }
            }

        yield {
            'packetType': 'success',
            'data': {
                'error': data
            }
        }

    @with_device_message
    def save_config(self, *args):  # pylint: disable=unused-argument
        '''
        Save configuration
        '''
        command_line = helper.build_input_packet('sC')
        # self.communicator.write(command_line)
        # result = self.get_input_result('sC', timeout=2)
        result = yield self._message_center.build(command=command_line, timeout=2)

        data = result['data']
        error = result['error']
        if data:
            yield {
                'packetType': 'success',
                'data': error
            }

        yield {
            'packetType': 'success',
            'data': error
        }

    @with_device_message
    def reset_params(self, params, *args):  # pylint: disable=unused-argument
        '''
        Reset params to default
        '''
        command_line = helper.build_input_packet('rD')
        result = yield self._message_center.build(command=command_line, timeout=2)

        error = result['error']
        data = result['data']
        if error:
            yield {
                'packetType': 'error',
                'data': {
                    'error': error
                }
            }

        yield {
            'packetType': 'success',
            'data': data
        }

    def upgrade_framework(self, params, *args):  # pylint: disable=unused-argument
        '''
        Upgrade framework
        '''
        file = ''
        if isinstance(params, str):
            file = params

        if isinstance(params, dict):
            file = params['file']

        # start a thread to do upgrade
        if not self.is_upgrading:
            self.is_upgrading = True
            self._message_center.pause()

            if self._logger is not None:
                self._logger.stop_user_log()

            thread = threading.Thread(
                target=self.thread_do_upgrade_framework, args=(file,))
            thread.start()
            print("Upgrade RTK330LA firmware started at:[{0}].".format(
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))

        return {
            'packetType': 'success'
        }

    @with_device_message
    def send_command(self, command_line):
        # command_line = #build a command
        # helper.build_input_packet('rD')
        result = yield self._message_center.build(command=command_line, timeout=5)

        error = result['error']
        data = result['data']
        if error:
            yield {
                'packetType': 'error',
                'data': {
                    'error': error
                }
            }

        yield {
            'packetType': 'success',
            'data': data
        }
