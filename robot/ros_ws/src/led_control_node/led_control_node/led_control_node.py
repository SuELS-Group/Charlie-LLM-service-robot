import json
from pathlib import Path

import rclpy
import usb.core
from rclpy.node import Node
from robot_common import get_config, get_logger
from std_msgs.msg import String

try:
    from pixel_ring import usb_pixel_ring_v2
except IOError as e:
    raise RuntimeError('Check the device is connected and recognized') from e
config = get_config('led_control_node', Path(__file__).stem)


class RespeakerInterface:

    def __init__(self, hardware, logger):
        self.hardware = hardware
        self.log = logger.sub('respeaker')
        self.dev = usb.core.find(idVendor=self.hardware.vendor_id, idProduct=self.hardware.product_id, )
        if not self.dev:
            raise RuntimeError('Failed to find Respeaker device')
        self.pixel_ring = usb_pixel_ring_v2.PixelRing(self.dev)
        self.log.info('Respeaker device connected')
        self.set_led_color(r=config.status.idle.color.r, g=config.status.idle.color.g, b=config.status.idle.color.b, a=config.status.idle.color.a, )

    def __del__(self):
        try:
            self.close()
        except:
            pass
        finally:
            self.dev = None

    def set_led_think(self):
        self.pixel_ring.set_brightness(self.hardware.think_brightness)
        self.pixel_ring.think()

    def set_led_trace(self):
        self.pixel_ring.set_brightness(self.hardware.trace_brightness)
        self.pixel_ring.trace()

    def set_led_spin(self):
        self.pixel_ring.set_brightness(self.hardware.spin_brightness)
        self.pixel_ring.spin()

    def set_led_color(self, r, g, b, a):
        self.pixel_ring.set_brightness(int(self.hardware.max_color_brightness * a))
        self.pixel_ring.set_color(r=int(r * 255), g=int(g * 255), b=int(b * 255))

    @property
    def version(self):
        return \
            self.dev.ctrl_transfer(usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE, 0, 0x80, 0, 1, self.hardware.timeout)[
                0]

    def close(self):
        usb.util.dispose_resources(self.dev)


class LedControlNode(Node):

    def __init__(self):
        super().__init__('led_control_node')
        self.log = get_logger()
        self.respeaker = RespeakerInterface(config.hardware, self.log)
        self.sub_led = self.create_subscription(String, config.topics.control_led, self.on_control_led, config.qos.subscription_depth)
        self.log.info(f'LED control ready on {config.topics.control_led}')

    def set_led_status_idle(self):
        self.respeaker.set_led_color(r=config.status.idle.color.r, g=config.status.idle.color.g, b=config.status.idle.color.b, a=config.status.idle.color.a, )

    def set_led_status_speech(self):
        self.respeaker.set_led_spin()

    def set_led_status_think(self):
        self.respeaker.set_led_think()

    def set_led_status_action(self):
        self.respeaker.set_led_color(r=config.status.action.color.r, g=config.status.action.color.g, b=config.status.action.color.b, a=config.status.action.color.a, )

    def set_led_status_unlock(self):
        self.set_led_status_idle()

    def on_shutdown(self):
        try:
            self.respeaker.close()
        except:
            pass
        finally:
            self.respeaker = None

    def on_control_led(self, msg):
        json_string = msg.data
        try:
            json_data = json.loads(json_string)
        except json.JSONDecodeError as e:
            self.log.warning(f'Ignoring invalid LED command payload: {e}')
            return
        status = json_data['status']
        self.log.info(f'Applying LED status {status}')
        match status:
            case 'idle':
                self.set_led_status_idle()
            case 'speech':
                self.set_led_status_speech()
            case 'trace':
                self.respeaker.set_led_trace()
            case 'thinking':
                self.set_led_status_think()
            case 'think':
                self.set_led_status_think()
            case 'action':
                self.set_led_status_action()
            case 'color':
                red = json_data['red']
                green = json_data['green']
                blue = json_data['blue']
                alpha = json_data['alpha']
                self.log.info(f'Applying custom LED color rgba=({red}, {green}, {blue}, {alpha})')
                self.respeaker.set_led_color(r=json_data['red'], g=json_data['green'], b=json_data['blue'], a=json_data[
                    'alpha'])
            case 'unlock':
                self.set_led_status_unlock()
            case _:
                self.log.warning(f'Unsupported LED status {status}')


def main():
    rclpy.init()
    led_control_node = LedControlNode()
    try:
        rclpy.spin(led_control_node)
    except KeyboardInterrupt:
        pass
    finally:
        led_control_node.on_shutdown()
        led_control_node.destroy_node()


if __name__ == '__main__':
    main()
