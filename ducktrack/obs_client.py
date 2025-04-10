import os
import subprocess
import time
import logging
from platform import system

import obsws_python as obs
import psutil

# Get logger
logger = logging.getLogger('DuckTrack.OBSClient')

def is_obs_running() -> bool:
    try:
        for process in psutil.process_iter(attrs=["pid", "name"]):
            if "obs" in process.info["name"].lower():
                return True
        return False
    except:
        raise Exception("Could not check if OBS is running already. Please check manually.")

def close_obs(obs_process: subprocess.Popen):
    try:
        if obs_process:
            if system() == "Darwin":
                # Use AppleScript to quit OBS gracefully on macOS
                try:
                    subprocess.run(['osascript', '-e', 'tell application "OBS" to quit'], 
                                  timeout=5, check=False)
                    # Double-check if it's still running
                    time.sleep(2)
                    if is_obs_running():
                        # Force kill as last resort
                        subprocess.run(['killall', 'OBS'], check=False)
                except Exception as e:
                    logger.error(f"Error gracefully closing OBS: {e}")
                    # As a last resort, kill the process
                    if obs_process:
                        obs_process.kill()
            else:
                # Windows and Linux
                obs_process.terminate()
                try:
                    obs_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error("OBS didn't terminate gracefully, forcing kill")
                    obs_process.kill()
                    
        # Wait for OBS to fully close
        time.sleep(1)
    except Exception as e:
        logger.error(f"Error closing OBS: {e}")
        # Last resort: try to kill it
        try:
            if system() == "Darwin":
                subprocess.run(['killall', 'OBS'], check=False)
            elif system() == "Windows":
                subprocess.run(['taskkill', '/F', '/IM', 'obs64.exe'], check=False)
                subprocess.run(['taskkill', '/F', '/IM', 'obs32.exe'], check=False)
            else:
                subprocess.run(['killall', 'obs'], check=False)
        except:
            pass

def find_obs() -> str:
    common_paths = {
        "Windows": [
            "C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe",
            "C:\\Program Files (x86)\\obs-studio\\bin\\32bit\\obs32.exe"
        ],
        "Darwin": [
            "/Applications/OBS.app/Contents/MacOS/OBS",
            "/opt/homebrew/bin/obs"
        ],
        "Linux": [
            "/usr/bin/obs",
            "/usr/local/bin/obs"
        ]
    }

    for path in common_paths.get(system(), []):
        if os.path.exists(path):
            return path
    
    try:
        if system() == "Windows":
            obs_path = subprocess.check_output("where obs", shell=True).decode().strip()
        else:
            obs_path = subprocess.check_output("which obs", shell=True).decode().strip()

        if os.path.exists(obs_path):
            return obs_path
    except subprocess.CalledProcessError:
        pass

    return "obs"

def open_obs() -> subprocess.Popen:
    try:
        obs_path = find_obs()
        
        if system() == "Windows":
            # you have to change the working directory first for OBS to find the correct locale on windows
            os.chdir(os.path.dirname(obs_path))
            obs_path = os.path.basename(obs_path)
            process = subprocess.Popen([obs_path, "--startreplaybuffer", "--minimize-to-tray"])
        elif system() == "Darwin":  # macOS specific handling
            # Use open command on macOS which handles permissions better than direct execution
            process = subprocess.Popen(["open", "-a", "OBS"])
            # Give OBS some time to initialize before trying to connect
            time.sleep(2)
        else:  # Linux
            process = subprocess.Popen([obs_path, "--startreplaybuffer", "--minimize-to-tray"])
            
        # Give OBS some time to start before returning
        time.sleep(1)
        return process
    except Exception as e:
        logger.error(f"Error launching OBS: {e}")
        raise Exception("Failed to find OBS, please open OBS manually.")

class OBSClient:
    """
    Controls the OBS client via the OBS websocket.
    Sets all the correct settings for recording.
    """
    
    def __init__(
        self, 
        recording_path: str, 
        metadata: dict, 
        fps=30,
        output_width=1280, 
        output_height=720, 
    ):
        self.metadata = metadata
        
        # Try to connect to OBS with a retry mechanism
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.info(f"Attempting to connect to OBS WebSocket (attempt {attempt+1}/{max_retries})")
                self.req_client = obs.ReqClient()
                self.event_client = obs.EventClient()
                break
            except Exception as e:
                logger.error(f"Failed to connect to OBS WebSocket: {e}")
                if attempt < max_retries - 1:
                    logger.info(f"Retrying in 2 seconds...")
                    time.sleep(2)
                else:
                    logger.error("Maximum retry attempts reached. Please ensure OBS is running with WebSocket enabled.")
                    raise Exception("Failed to connect to OBS WebSocket after multiple attempts")
        
        self.record_state_events = {}
        
        def on_record_state_changed(data):
            output_state = data.output_state
            logger.info("record state changed: %s", output_state)
            if output_state not in self.record_state_events:
                self.record_state_events[output_state] = []
            self.record_state_events[output_state].append(time.perf_counter())
        
        self.event_client.callback.register(on_record_state_changed)

        try:
            # First, check if OBS is ready to accept WebSocket commands
            try:
                version = self.req_client.get_version()
                logger.info("Connected to OBS version: %s", version.obs_version)
            except Exception as e:
                logger.warning("OBS is running but WebSocket might not be ready: %s", e)
                # Wait a bit longer for WebSocket to initialize
                time.sleep(3)
            
            # Now proceed with profile operations
            self.old_profile = self.req_client.get_profile_list().current_profile_name

            # Profile creation/management with error handling
            try:
                profiles = self.req_client.get_profile_list().profiles
                if "computer_tracker" not in profiles:
                    self.req_client.create_profile("computer_tracker")
                
                # Set to computer_tracker profile
                self.req_client.set_current_profile("computer_tracker")
            except obs.error.OBSSDKRequestError as e:
                logger.warning("Warning: Unable to create or switch to the 'computer_tracker' profile: %s", e)
                # Continue with the current profile
                logger.info("Continuing with the current OBS profile")

            # Configure the video settings regardless of profile
            base_width = metadata["screen_width"]
            base_height = metadata["screen_height"]
            
            if metadata["system"] == "Darwin":
                # for retina displays
                # TODO: check if external displays are messed up by this
                base_width *= 2
                base_height *= 2
            
            scaled_width, scaled_height = _scale_resolution(base_width, base_height, output_width, output_height)
            
            self.req_client.set_profile_parameter("Video", "BaseCX", str(base_width))
            self.req_client.set_profile_parameter("Video", "BaseCY", str(base_height))
            self.req_client.set_profile_parameter("Video", "OutputCX", str(scaled_width))
            self.req_client.set_profile_parameter("Video", "OutputCY", str(scaled_height))
            self.req_client.set_profile_parameter("Video", "ScaleType", "lanczos")

            self.req_client.set_profile_parameter("AdvOut", "RescaleRes", f"{base_width}x{base_height}")
            self.req_client.set_profile_parameter("AdvOut", "RecRescaleRes", f"{base_width}x{base_height}")
            self.req_client.set_profile_parameter("AdvOut", "FFRescaleRes", f"{base_width}x{base_height}")

            self.req_client.set_profile_parameter("Video", "FPSCommon", str(fps))
            self.req_client.set_profile_parameter("Video", "FPSInt", str(fps))
            self.req_client.set_profile_parameter("Video", "FPSNum", str(fps))
            self.req_client.set_profile_parameter("Video", "FPSDen", "1")
            
            self.req_client.set_profile_parameter("SimpleOutput", "RecFormat2", "mp4")
            
            bitrate = int(_get_bitrate_mbps(scaled_width, scaled_height, fps=fps) * 1000 / 50) * 50
            self.req_client.set_profile_parameter("SimpleOutput", "VBitrate", str(bitrate))
            
            # do this in order to get pause & resume
            self.req_client.set_profile_parameter("SimpleOutput", "RecQuality", "Small")

            self.req_client.set_profile_parameter("SimpleOutput", "FilePath", recording_path)
        
            # TODO: not all OBS configs have this, maybe just instruct the user to mute themselves


            try:
                self.req_client.set_input_mute("Mic/Aux", muted=True)
            except obs.error.OBSSDKRequestError :
                # In case there is no Mic/Aux input, this will throw an error
                pass
        except obs.error.OBSSDKRequestError as e:
            logger.warning("Warning: Unable to get current profile: %s", e)
            # Continue with the current profile
            logger.info("Continuing with the current OBS profile")

    def start_recording(self):
        self.req_client.start_record()

    def stop_recording(self):
        self.req_client.stop_record()
        
        # Only try to restore the old profile if it was successfully stored during initialization
        if hasattr(self, 'old_profile'):
            try:
                self.req_client.set_current_profile(self.old_profile) # restore old profile
            except obs.error.OBSSDKRequestError as e:
                logger.warning("Warning: Unable to restore original profile: %s", e)

    def pause_recording(self):
        self.req_client.pause_record()
    
    def resume_recording(self):
        self.req_client.resume_record()
   
def _get_bitrate_mbps(width: int, height: int, fps=30) -> float:
    """
    Gets the YouTube recommended bitrate in Mbps for a given resolution and framerate.
    Refer to https://support.google.com/youtube/answer/1722171?hl=en#zippy=%2Cbitrate
    """
    resolutions = {
        (7680, 4320): {30: 120, 60: 180},
        (3840, 2160): {30: 40,  60: 60.5},
        (2160, 1440): {30: 16,  60: 24},
        (1920, 1080): {30: 8,   60: 12},
        (1280, 720):  {30: 5,   60: 7.5},
        (640, 480):   {30: 2.5, 60: 4},
        (480, 360):   {30: 1,   60: 1.5}
    }

    if (width, height) in resolutions:
        return resolutions[(width, height)].get(fps)
    else:
        # approximate the bitrate using a simple linear model
        area = width * height
        multiplier = 3.5982188179592543e-06 if fps == 30 else 5.396175171097084e-06
        constant = 2.418399836285939 if fps == 30 else 3.742780056500365
        return multiplier * area + constant

def _scale_resolution(base_width: int, base_height: int, target_width: int,  target_height: int) -> tuple[int, int]:
    target_area = target_width * target_height
    aspect_ratio = base_width / base_height
    
    scaled_height = int((target_area / aspect_ratio) ** 0.5)
    scaled_width = int(aspect_ratio * scaled_height)
    
    return scaled_width, scaled_height