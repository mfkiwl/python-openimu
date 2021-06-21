import time
import json
import serial
import serial.tools.list_ports

from ...framework.context import APP_CONTEXT
from ..base.rtk_provider_base import RTKProviderBase

from ..upgrade_workers import (
    FirmwareUpgradeWorker,
    UPGRADE_EVENT,
    SDK8100UpgradeWorker
)
from ...framework.utils.print import (print_green, print_yellow, print_red)


class Provider(RTKProviderBase):
    '''
    OpenRTK UART provider
    '''

    def __init__(self, communicator, *args):
        super(Provider, self).__init__(communicator)
        self.bootloader_baudrate = 115200
        self.config_file_name = 'openrtk.json'
        self.device_category = 'OpenRTK'
        self.port_index_define = {
            'user': 0,
            'rtcm': 1,
            'debug': 2,
        }

    def thread_debug_port_receiver(self, *args, **kwargs):
        if self.debug_logf is None:
            return

        # is_get_configuration = 0
        # file_name = args[0]
        # self.debug_c_f = open(file_name + '/' + 'configuration.json', "w")

        # while True:
        #     if is_get_configuration:
        #         break
        #     cmd_configuration = 'get configuration\r\n'
        #     self.debug_serial_port.write(cmd_configuration.encode())
        #     try_times = 20
        #     for i in range(try_times):
        #         data_buffer = self.debug_serial_port.read(700)
        #         if len(data_buffer):
        #             try:
        #                 # print('len = {0}'.format(len(data_buffer)))
        #                 str_data = bytes.decode(data_buffer)
        #                 # print('{0}'.format(str_data))
        #                 json_data = json.loads(str_data)
        #                 for key in json_data.keys():
        #                     if key == 'openrtk configuration':
        #                         APP_CONTEXT.get_print_logger().info(
        #                             '{0}'.format(json_data))
        #                         if self.debug_c_f:
        #                             self.debug_c_f.write(str_data)
        #                             self.debug_c_f.close()
        #                         is_get_configuration = 1
        #                 if is_get_configuration:
        #                     break
        #             except Exception as e:
        #                 # print('DEBUG PORT Thread:json error:', e)
        #                 # the json will not be completed
        #                 pass

        cmd_log = 'log debug on\r\n'
        self.debug_serial_port.write(cmd_log.encode())

        # log data
        while True:
            try:
                data = bytearray(self.debug_serial_port.read_all())
            except Exception as e:
                print_red('DEBUG PORT Thread error: {0}'.format(e))
                return  # exit thread receiver
            if data and len(data) > 0:
                self.debug_logf.write(data)
            else:
                time.sleep(0.001)

    def thread_rtcm_port_receiver(self, *args, **kwargs):
        if self.rtcm_logf is None:
            return
        while True:
            try:
                data = bytearray(self.rtcm_serial_port.read_all())
            except Exception as e:
                print_red('RTCM PORT Thread error: {0}'.format(e))
                return  # exit thread receiver
            if len(data):
                self.rtcm_logf.write(data)
            else:
                time.sleep(0.001)

    # override
    def build_worker(self, rule, content):
        if rule == 'rtk':
            firmware_worker = FirmwareUpgradeWorker(
                self.communicator, self.bootloader_baudrate, content)
            firmware_worker.on(
                UPGRADE_EVENT.FIRST_PACKET, lambda: time.sleep(8))
            return firmware_worker

        if rule == 'sdk':
            sdk_worker = SDK8100UpgradeWorker(
                self.communicator, self.bootloader_baudrate, content)
            return sdk_worker

    # command list
    # use base methods
