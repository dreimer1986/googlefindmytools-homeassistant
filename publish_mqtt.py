import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict
from dateutil.parser import parse

import paho.mqtt.client as mqtt
import petname

from auth.fcm_receiver import FcmReceiver
from NovaApi.ExecuteAction.LocateTracker.location_request import (
    get_location_data_for_device,
)
from NovaApi.ListDevices.nbe_list_devices import request_device_list
from ProtoDecoders.decoder import get_canonic_ids, parse_device_list_protobuf

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("GoogleFindMyTools")

# --- Environment Variable for Semantic Location Mapping ---
SEMANTIC_MAPPING_STR = os.environ.get("SEMANTIC_MAPPING", "")
SEMANTIC_TO_STATE = {}
if SEMANTIC_MAPPING_STR:
    try:
        SEMANTIC_TO_STATE = dict(
            item.split(":", 1) for item in SEMANTIC_MAPPING_STR.split(";")
        )
    except ValueError:
        logger.error(
            f'Invalid format for SEMANTIC_MAPPING: "{SEMANTIC_MAPPING_STR}". Expected format: "key1:value1;key2:value2".'
        )

# MQTT Configuration
MQTT_BROKER = "192.168.181.42"
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USERNAME = "mqtt"  # Set your MQTT username if required
MQTT_PASSWORD = "12341234"  # Set your MQTT password if required
MQTT_CLIENT_ID = f"{os.environ.get('MQTT_CLIENT_ID', 'google_find_my_publisher')}_{petname.Generate(3, '')}"
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", 300))  # Default: 300s (5 minutes)

# Optional: Filter for specific device names (comma-separated)
DEVICE_NAMES_FILTER = os.environ.get("DEVICE_NAMES_FILTER")

# Home Assistant MQTT Discovery
DISCOVERY_PREFIX = "homeassistant"
DEVICE_PREFIX = "google_find_my"


def on_connect(client, userdata, flags, result_code, properties):
    """Callback when connected to MQTT broker"""
    if result_code == 0:
        logger.info("Successfully connected to the MQTT broker.")
    else:
        logger.error(
            f"Failed to connect to the MQTT broker. Result code: {result_code}"
        )


def publish_device_config(
    client: mqtt.Client, device_name: str, canonic_id: str
) -> None:
    """Publish Home Assistant MQTT discovery configuration for a device"""
    base_topic = f"{DISCOVERY_PREFIX}/device_tracker/{DEVICE_PREFIX}_{canonic_id}"

    # Device configuration for Home Assistant
    config = {
        "unique_id": f"{DEVICE_PREFIX}_{canonic_id}",
        "state_topic": f"{base_topic}/state",
        "json_attributes_topic": f"{base_topic}/attributes",
        "source_type": "gps",
        "device": {
            "identifiers": [f"{DEVICE_PREFIX}_{canonic_id}"],
            "name": device_name,
            "model": "Google Find My Device",
            "manufacturer": "Google",
        },
    }
    logger.info(
        f"Publishing discovery configuration for '{device_name}' (ID: {canonic_id}) to topic '{base_topic}/config'."
    )
    # Publish discovery config
    r = client.publish(f"{base_topic}/config", json.dumps(config), retain=True)
    return r

def get_semantic_location(device_name: str, semantic_location: str, lat: float, lon: float) -> tuple[str | None, float, float]:
    """Convert semantic location to state or coordinates"""
    if not semantic_location or not (mapped_value := SEMANTIC_TO_STATE.get(semantic_location)):
        return None, lat, lon

    # Check if the mapped value is coordinates
    if ',' in mapped_value:
        try:
            new_lat, new_lon = map(float, mapped_value.split(','))
            logger.info(f"Device '{device_name}' location '{semantic_location}' -> coordinates ({new_lat}, {new_lon})")
            return None, new_lat, new_lon
        except ValueError:
            logger.warning(f"Invalid coordinates for semantic location '{semantic_location}': {mapped_value}")
            return None, lat, lon
    
    # Handle state case
    logger.info(f"Device '{device_name}' location '{semantic_location}' -> state '{mapped_value}'")
    return mapped_value, lat, lon
            

def publish_device_state(
    client: mqtt.Client, device_name: str, canonic_id: str, location_data: Dict
) -> None:
    """Publish device state and attributes to MQTT"""
    base_topic = f"{DISCOVERY_PREFIX}/device_tracker/{DEVICE_PREFIX}_{canonic_id}"

    # Extract location data
    accuracy = location_data.get("accuracy")
    altitude = location_data.get("altitude")
    timestamp = location_data.get("timestamp")
    semantic_location = location_data.get("semantic_location")

    # Determine state based on semantic_location
    state, lat, lon = get_semantic_location(device_name, semantic_location, location_data.get("latitude"), location_data.get("longitude"))
    client.publish(f"{base_topic}/state", state)

    last_updated_iso = get_timestamp(timestamp)
    
    # Publish attributes
    attributes = {
        "source_type": "gps",
        "last_updated": last_updated_iso,
    }
    
    # Add GPS attributes if they exist and are not None/empty
    if lat is not None:
        attributes["latitude"] = lat
    if lon is not None:
        attributes["longitude"] = lon
    if altitude is not None:
        attributes["altitude"] = altitude
    if accuracy is not None:
        attributes["gps_accuracy"] = accuracy
    
    # Add semantic_location to attributes if it exists
    if semantic_location:
        attributes["semantic_location"] = semantic_location
        logger.info(
            f"Publishing location for '{device_name}' (ID: {canonic_id}) with semantic location: {semantic_location}"
        )
    else:
        logger.info(
            f"Publishing location for '{device_name}' (ID: {canonic_id}): "
            f"lat={lat}, lon={lon}, accuracy={accuracy}"
        )
    
    r = client.publish(f"{base_topic}/attributes", json.dumps(attributes))
    return r

def get_timestamp(timestamp: int) -> str:
    if timestamp:
        if isinstance(timestamp, (int, float)):
            dt_obj = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            # Otherwise, parse the timestamp string
            dt_obj = parse(str(timestamp))
            # If the parsed timestamp has no timezone, assume it's in local time
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.astimezone()

        last_updated_iso = dt_obj.isoformat()
        return last_updated_iso

def main():
    # Initialize MQTT client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, MQTT_CLIENT_ID)
    client.on_connect = on_connect

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    fcm_receiver = FcmReceiver()
    try:
        logger.info("Connecting to the MQTT broker...")
        client.connect(MQTT_BROKER, MQTT_PORT)
        client.loop_start()
        
        while True:
            try:
                logger.info("Starting new update cycle...")
                logger.info("Retrieving device list from Google Find My Device API...")
                result_hex = request_device_list()
                device_list = parse_device_list_protobuf(result_hex)
                canonic_ids = get_canonic_ids(device_list)

                # Filter devices if a filter is set
                if DEVICE_NAMES_FILTER:
                    # Create a set of allowed names, stripping whitespace and ignoring any empty entries.
                    allowed_names = {name.strip() for name in DEVICE_NAMES_FILTER.split(',') if name.strip()}
                    if allowed_names:
                        logger.info(f"Filtering for devices with names: {', '.join(sorted(list(allowed_names)))}")
                        canonic_ids = [
                            (name, cid) for name, cid in canonic_ids if name in allowed_names
                        ]

                logger.info(f"Found {len(canonic_ids)} device(s) to publish.")

                # Publish discovery config and state for each device
                for device_name, canonic_id in canonic_ids:
                    try:
                        logger.info(f"Processing device '{device_name}' (ID: {canonic_id})...")
                        # Publish discovery configuration
                        msg_info = publish_device_config(client, device_name, canonic_id)
                        msg_info.wait_for_publish()

                        # Get and publish location data
                        location_data = get_location_data_for_device(fcm_receiver, canonic_id, device_name)
                        if not location_data:
                            logger.warning(f"Incomplete or missing location data for '{device_name}'. Skipping.")
                            continue

                        # Check if we have either GPS coordinates or semantic location
                        has_gps = location_data.get('latitude') is not None and location_data.get('longitude') is not None
                        has_semantic = location_data.get('semantic_location') is not None

                        if not has_gps and not has_semantic:
                            logger.warning(f"No usable location data (GPS or semantic) for '{device_name}'. Skipping.")
                            continue

                        msg_info = publish_device_state(client, device_name, canonic_id, location_data)
                        msg_info.wait_for_publish()
                        logger.info(f"Finished publishing data for '{device_name}'.")
                    except Exception as e:
                        logger.error(f"Failed to process device '{device_name}': {e}. Continuing to next device.")

            except Exception as e:
                logger.error(f"Failed to complete update cycle: {e}")

            logger.info("Update cycle complete.")
            logger.info(f"Waiting {REFRESH_INTERVAL} seconds for the next cycle...")
            time.sleep(REFRESH_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Script interrupted by user. Shutting down.")
    except Exception as e:
        logger.error(f"An unrecoverable error occurred: {e}")
    finally:
        fcm_receiver.stop_listening()
        client.loop_stop()
        client.disconnect()
        logger.info("Disconnected from the MQTT broker.")


if __name__ == "__main__":
    main()
