import json
import os
import time
import subprocess
import logging
import threading
from datetime import datetime
from platform import system
from queue import Queue, Empty

from pynput import keyboard, mouse
from pynput.keyboard import KeyCode
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from .metadata import MetadataManager
from .obs_client import OBSClient
from .util import fix_windows_dpi_scaling, get_recordings_dir

# Get the logger
logger = logging.getLogger('DuckTrack.Recorder')

class MacOSInputMonitor:
    """Alternative input monitor for macOS using periodic sentinel events as fallback."""
    
    def __init__(self, on_click=None, on_key=None):
        self.running = False
        self.on_click = on_click
        self.on_key = on_key
        self.monitor_thread = None
        self.permission_check_count = 0
        self.max_permission_checks = 3
        
    def start(self):
        """Start the monitoring thread."""
        self.running = True
        self.monitor_thread = threading.Thread(target=self._run_monitor)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()
        logger.info("Started macOS fallback input monitor")
        return True
        
    def stop(self):
        """Stop the monitoring thread."""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1.0)
        logger.info("Stopped macOS fallback input monitor")
        
    def _run_monitor(self):
        """Run a basic monitor that adds periodic sentinel events."""
        logger.info("MacOS monitor thread started")
        
        last_sentinel_time = time.time()
        
        while self.running:
            try:
                current_time = time.time()
                
                # Try to get mouse position only a few times, then give up to avoid log spam
                if self.permission_check_count < self.max_permission_checks:
                    try:
                        # Try to get mouse position using AppleScript
                        x, y = self._get_mouse_position()
                        # If we succeeded, report the position
                        if x > 0 or y > 0:
                            logger.info(f"Successfully detected mouse at ({x}, {y})")
                            if self.on_click:
                                self.on_click(x, y, None, None, event_type="move")
                    except Exception as e:
                        logger.warning(f"Mouse position detection failed: {e}")
                        self.permission_check_count += 1
                        if self.permission_check_count >= self.max_permission_checks:
                            logger.warning("Giving up on mouse position detection after multiple failures")
                
                # Add a sentinel event every 2 seconds
                if current_time - last_sentinel_time > 2:
                    # Create a sentinel event with the current timestamp
                    if self.on_click:
                        self.on_click(0, 0, None, None, event_type="sentinel")
                    
                    # Log periodically
                    if (int(current_time) % 10) < 1:
                        logger.info("MacOS fallback monitor adding sentinel events")
                    
                    last_sentinel_time = current_time
                
                # Sleep briefly to avoid using too much CPU
                time.sleep(0.5)
                
            except Exception as e:
                logger.error(f"Error in macOS monitor: {e}")
                time.sleep(1.0)
    
    def _get_mouse_position(self):
        """Get the current mouse position using AppleScript, with better error handling."""
        try:
            # Use a different AppleScript approach that might have better compatibility
            script = 'tell application "System Events"\ntry\nget the position of the mouse\non error\nreturn {0, 0}\nend try\nend tell'
            result = subprocess.run(['osascript', '-e', script], 
                                   capture_output=True, text=True, check=False)
            
            # Check return code
            if result.returncode != 0:
                logger.warning(f"AppleScript failed with return code {result.returncode}: {result.stderr}")
                return 0, 0
                
            if result.stdout:
                parts = result.stdout.strip().split(', ')
                if len(parts) == 2:
                    return int(parts[0]), int(parts[1])
            return 0, 0
        except Exception as e:
            logger.error(f"Error getting mouse position: {e}")
            return 0, 0

def check_macos_permissions():
    """Check and prompt for permissions on macOS."""
    if system() != "Darwin":
        return True
    
    # Check for Accessibility permissions
    try:
        # This command checks if the app has accessibility permissions
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events"\ntry\nget the position of the mouse\non error\nreturn {0, 0}\nend try\nend tell'],
            capture_output=True, timeout=1
        )
        
        permissions_granted = True
        if result.returncode != 0:
            logger.warning("Accessibility permissions not granted")
            permissions_granted = False
            
            # Show a more detailed explanation about permissions
            QMessageBox.warning(
                None, 
                "Accessibility Permissions Required",
                "DuckTrack needs Accessibility permissions to track mouse movements and keystrokes.\n\n"
                "Without these permissions, DuckTrack will still record your screen,\n"
                "but won't be able to capture mouse and keyboard actions.\n\n"
                "To grant permissions:\n"
                "1. Go to System Settings > Privacy & Security > Accessibility\n"
                "2. Click the '+' button and add DuckTrack\n"
                "3. Make sure the checkbox next to DuckTrack is enabled\n\n"
                "You may need to restart DuckTrack after changing permissions."
            )
            return False
            
        # Try to test pynput specifically to detect the TypeError issue
        try:
            logger.info("Testing if pynput has the ThreadHandle issue...")
            # Create a short-lived test listener
            test_listener = mouse.Listener(on_move=lambda x, y: None)
            test_listener.start()
            time.sleep(0.1)
            test_listener.stop()
            logger.info("pynput test completed without errors")
            return True
        except TypeError as e:
            # Catch the specific TypeError we see in the logs
            if "_ThreadHandle" in str(e) and "not callable" in str(e):
                logger.warning(f"Detected pynput ThreadHandle error: {e}")
                QMessageBox.information(
                    None,
                    "Input Capture Limitation",
                    "DuckTrack has detected a compatibility issue with input capture on your version of macOS.\n\n"
                    "We'll still record your screen, but detailed input events won't be captured.\n\n"
                    "This is a known limitation with the input library on newer macOS versions."
                )
                return False
            raise  # Re-raise if it's a different TypeError
        
    except Exception as e:
        logger.error(f"Error checking permissions: {e}")
        QMessageBox.warning(
            None, 
            "Permissions Required",
            "DuckTrack needs Accessibility permissions to record keyboard and mouse events.\n\n"
            "Without these permissions, only the screen will be recorded.\n\n"
            "To grant permissions, go to System Settings > Privacy & Security > Accessibility\n"
            "and make sure DuckTrack is allowed."
        )
        return False


class Recorder(QThread):
    """
    Makes recordings.
    """
    
    recording_stopped = pyqtSignal()

    def __init__(self, natural_scrolling: bool):
        super().__init__()
        
        if system() == "Windows":
            fix_windows_dpi_scaling()
        
        # Check permissions on macOS and detect pynput compatibility issues
        self.use_fallback = False
        self.thread_handle_error_detected = False
        
        if system() == "Darwin":
            # Try a quick test of pynput to catch the ThreadHandle error before proceeding
            try:
                test_listener = mouse.Listener(on_move=lambda x, y: None)
                test_listener.start()
                time.sleep(0.1)  # Give it a moment to fail if it's going to
                test_listener.stop()
                logger.info("Initial pynput test passed")
                
                # Continue with normal permission check
                has_permissions = check_macos_permissions()
                if not has_permissions:
                    logger.warning("Missing required permissions for event recording")
                    self.use_fallback = True
            except TypeError as e:
                # Check specifically for the ThreadHandle error
                if "_ThreadHandle" in str(e) and "not callable" in str(e):
                    logger.error(f"Detected pynput ThreadHandle compatibility issue: {e}")
                    self.thread_handle_error_detected = True
                    self.use_fallback = True
                    
                    # Inform the user about the limitation
                    QMessageBox.information(
                        None,
                        "Input Capture Limitation",
                        "DuckTrack has detected a compatibility issue with input capture on your version of macOS.\n\n"
                        "We'll still record your screen, but detailed input events won't be captured.\n\n"
                        "This is a known limitation with the input library on newer macOS versions."
                    )
                else:
                    # If it's a different TypeError, still do the permission check
                    logger.warning(f"Unknown TypeError during pynput test: {e}")
                    has_permissions = check_macos_permissions()
                    if not has_permissions:
                        self.use_fallback = True
            except Exception as e:
                logger.warning(f"Error in pynput test: {e}")
                # Still try the permission check
                has_permissions = check_macos_permissions()
                if not has_permissions:
                    self.use_fallback = True
        
        self.recording_path = self._get_recording_path()
        
        self._is_recording = False
        self._is_paused = False
        
        self.event_queue = Queue()
        self.events_file = open(os.path.join(self.recording_path, "events.jsonl"), "a")
        
        self.metadata_manager = MetadataManager(
            recording_path=self.recording_path, 
            natural_scrolling=natural_scrolling
        )
        self.obs_client = OBSClient(recording_path=self.recording_path, 
                                    metadata=self.metadata_manager.metadata)

        # Create listeners with try/except to catch permission issues
        try:
            if system() == "Darwin" and (self.use_fallback or self.thread_handle_error_detected):
                # Use the fallback method on macOS if we need to
                if self.thread_handle_error_detected:
                    logger.info("Using macOS fallback input monitor due to pynput compatibility issue")
                else:
                    logger.info("Using macOS fallback input monitor due to permission issues")
                
                self.macos_monitor = MacOSInputMonitor(
                    on_click=self.macos_on_input
                )
                # We won't be using pynput listeners in this case
                self.mouse_listener = None
                self.keyboard_listener = None
            else:
                # Use standard pynput listeners (default case)
                logger.info("Initializing mouse listener...")
                self.mouse_listener = mouse.Listener(
                    on_move=self.on_move,
                    on_click=self.on_click,
                    on_scroll=self.on_scroll)
                
                logger.info("Initializing keyboard listener...")
                self.keyboard_listener = keyboard.Listener(
                    on_press=self.on_press, 
                    on_release=self.on_release)
                
                logger.info("Testing listeners...")
                try:
                    test_mouse = mouse.Listener(on_move=lambda x, y: None)
                    test_mouse.start()
                    test_mouse.stop()
                    
                    test_keyboard = keyboard.Listener(on_press=lambda k: None)
                    test_keyboard.start()
                    test_keyboard.stop()
                    
                    logger.info("Input listeners successfully initialized")
                except TypeError as e:
                    # If we get the ThreadHandle error during testing, switch to fallback
                    if "_ThreadHandle" in str(e) and "not callable" in str(e):
                        logger.error(f"Cannot use standard listeners due to error: {e}")
                        self.thread_handle_error_detected = True
                        self.use_fallback = True
                        
                        logger.info("Switching to macOS fallback input monitor")
                        self.macos_monitor = MacOSInputMonitor(
                            on_click=self.macos_on_input
                        )
                        self.mouse_listener = None
                        self.keyboard_listener = None
                    else:
                        raise
        except Exception as e:
            logger.error(f"ERROR initializing input listeners: {e}")
            # Switch to fallback method on macOS
            if system() == "Darwin":
                logger.info("Switching to macOS fallback input monitor due to error")
                self.macos_monitor = MacOSInputMonitor(
                    on_click=self.macos_on_input
                )
                self.mouse_listener = None
                self.keyboard_listener = None
                self.use_fallback = True
            else:
                # Still allow recording to proceed, but warn user
                QMessageBox.warning(
                    None,
                    "Input Recording Issue",
                    "Could not initialize keyboard and mouse monitoring.\n\n"
                    "Your screen will be recorded, but keyboard and mouse events won't be tracked.\n"
                    "Check accessibility permissions in your system settings."
                )
    
    def macos_on_input(self, x, y, button=None, pressed=None, event_type="click"):
        """Handle input events from the macOS fallback monitor."""
        if not self._is_paused and self._is_recording:
            try:
                # Create an event based on the type
                if event_type == "move" and (x > 0 or y > 0):
                    event = {
                        "time_stamp": time.perf_counter(),
                        "action": "move",
                        "x": x,
                        "y": y
                    }
                    logger.info(f"MacOS fallback event: mouse move to ({x}, {y})")
                elif event_type == "click":
                    event = {
                        "time_stamp": time.perf_counter(),
                        "action": "click",
                        "x": x,
                        "y": y,
                        "button": button if button else "unknown",
                        "pressed": pressed if pressed is not None else True
                    }
                    logger.info(f"MacOS fallback event: click at ({x}, {y})")
                elif event_type == "sentinel":
                    # Create a sentinel event
                    event = {
                        "time_stamp": time.perf_counter(),
                        "action": "sentinel",
                        "timestamp": time.time()
                    }
                    # Don't log sentinel events too often to avoid log spam
                else:
                    # Some other type of event
                    event = {
                        "time_stamp": time.perf_counter(),
                        "action": event_type,
                        "x": x,
                        "y": y
                    }
                    logger.info(f"MacOS fallback event: {event_type} at ({x}, {y})")
                
                self.event_queue.put(event, block=False)
            except Exception as e:
                logger.error(f"Error in macOS fallback input handler: {e}")
    
    def on_move(self, x, y):
        if not self._is_paused and self._is_recording:
            try:
                # Debug every nth move event to avoid too much output
                if not hasattr(self, '_move_counter'):
                    self._move_counter = 0
                
                self._move_counter += 1
                if self._move_counter % 100 == 0:  # Only log every 100th move event
                    logger.info(f"Mouse moved to ({x}, {y})")
                
                event = {"time_stamp": time.perf_counter(), 
                        "action": "move", 
                        "x": x, 
                        "y": y}
                self.event_queue.put(event, block=False)
            except Exception as e:
                logger.error(f"Error capturing mouse move: {e}")
        
    def on_click(self, x, y, button, pressed):
        if not self._is_paused and self._is_recording:
            try:
                # Print debug info for mouse clicks with timestamp
                logger.info(f"[{time.strftime('%H:%M:%S')}] Mouse click: x={x}, y={y}, button={button.name}, pressed={pressed}")
                
                event = {"time_stamp": time.perf_counter(), 
                        "action": "click", 
                        "x": x, 
                        "y": y, 
                        "button": button.name, 
                        "pressed": pressed}
                self.event_queue.put(event, block=False)
            except Exception as e:
                logger.error(f"Error capturing mouse click: {e}")
    
    def on_scroll(self, x, y, dx, dy):
        if not self._is_paused and self._is_recording:
            try:
                event = {"time_stamp": time.perf_counter(), 
                        "action": "scroll", 
                        "x": x, 
                        "y": y, 
                        "dx": dx, 
                        "dy": dy}
                self.event_queue.put(event, block=False)
            except Exception as e:
                logger.error(f"Error capturing scroll: {e}")
    
    def on_press(self, key):
        if not self._is_paused and self._is_recording:
            try:
                key_name = key.char if hasattr(key, 'char') and key.char is not None else key.name
                # Print debug info for key presses with timestamp
                logger.info(f"[{time.strftime('%H:%M:%S')}] Key press: {key_name}")
                
                event = {"time_stamp": time.perf_counter(), 
                        "action": "press", 
                        "name": key_name}
                self.event_queue.put(event, block=False)
            except Exception as e:
                logger.error(f"Error capturing key press: {e}")

    def on_release(self, key):
        if not self._is_paused and self._is_recording:
            try:
                key_name = key.char if hasattr(key, 'char') and key.char is not None else key.name
                
                event = {"time_stamp": time.perf_counter(), 
                        "action": "release", 
                        "name": key_name}
                self.event_queue.put(event, block=False)
            except Exception as e:
                logger.error(f"Error capturing key release: {e}")

    def run(self):
        self._is_recording = True
        
        self.metadata_manager.collect()
        self.obs_client.start_recording()
        
        # Add a startup event for debugging
        start_event = {"time_stamp": time.perf_counter(), "action": "recording_started"}
        self.events_file.write(json.dumps(start_event) + "\n")
        self.events_file.flush()  # Ensure it's written to disk immediately
        
        logger.info(f"Starting recording to {self.recording_path}")
        
        # Determine which input capture method we're using
        capture_method = "unknown"
        if system() == "Darwin" and (self.use_fallback or self.thread_handle_error_detected):
            if self.thread_handle_error_detected:
                capture_method = "macOS_fallback_due_to_pynput_error"
            else:
                capture_method = "macOS_fallback_due_to_permissions"
        elif hasattr(self, 'mouse_listener') and hasattr(self, 'keyboard_listener') and self.mouse_listener and self.keyboard_listener:
            capture_method = "pynput_listeners"
        else:
            capture_method = "none"
        
        # Add event for the capture method
        method_event = {"time_stamp": time.perf_counter(), "action": "input_capture_method", "method": capture_method}
        self.events_file.write(json.dumps(method_event) + "\n")
        self.events_file.flush()
        
        # Check if listeners are defined and start them
        listeners_working = False  # Default to assuming not working
        try:
            if capture_method.startswith("macOS_fallback"):
                # Start the macOS fallback monitor
                logger.info(f"Starting macOS fallback input monitor: {capture_method}")
                self.macos_monitor.start()
                
                # Add an event to show fallback method is being used
                fallback_event = {"time_stamp": time.perf_counter(), "action": "macos_fallback_monitor_started", "reason": capture_method}
                self.events_file.write(json.dumps(fallback_event) + "\n")
                self.events_file.flush()
                listeners_working = True  # The fallback monitor should work
            elif capture_method == "pynput_listeners":
                try:
                    logger.info("Starting pynput input listeners...")
                    self.mouse_listener.start()
                    self.keyboard_listener.start()
                    
                    # Check if listeners are actually working
                    if self.mouse_listener.running and self.keyboard_listener.running:
                        listeners_working = True
                        logger.info("pynput listeners started successfully")
                    else:
                        listeners_working = False
                        logger.warning("pynput listeners failed to start properly")
                    
                    # Add an event to show listeners started
                    listener_event = {"time_stamp": time.perf_counter(), 
                                    "action": "input_listeners_started",
                                    "mouse_running": self.mouse_listener.running,
                                    "keyboard_running": self.keyboard_listener.running}
                    self.events_file.write(json.dumps(listener_event) + "\n")
                    self.events_file.flush()
                except TypeError as e:
                    # If we get the ThreadHandle error at this stage, try to recover
                    if "_ThreadHandle" in str(e) and "not callable" in str(e):
                        logger.error(f"pynput ThreadHandle error when starting listeners: {e}")
                        # Switch to fallback if we're on macOS
                        if system() == "Darwin" and not hasattr(self, 'macos_monitor'):
                            logger.info("Creating fallback monitor after pynput failure")
                            self.macos_monitor = MacOSInputMonitor(
                                on_click=self.macos_on_input
                            )
                            self.macos_monitor.start()
                            listeners_working = True
                            
                            # Add an event about the recovery
                            recovery_event = {"time_stamp": time.perf_counter(), 
                                            "action": "recovered_with_fallback_monitor", 
                                            "error": str(e)}
                            self.events_file.write(json.dumps(recovery_event) + "\n")
                            self.events_file.flush()
                    else:
                        # Different error
                        logger.error(f"Error starting pynput listeners: {e}")
                        listeners_working = False
            else:
                logger.warning("WARNING: No input listeners available, only recording screen")
                listeners_working = False
                
                # Add an event to show listeners are not available
                no_listener_event = {"time_stamp": time.perf_counter(), "action": "input_listeners_unavailable"}
                self.events_file.write(json.dumps(no_listener_event) + "\n")
                self.events_file.flush()
            
            # Log that recording has started and inputs are being captured
            heartbeat_event = {"time_stamp": time.perf_counter(), 
                             "action": "heartbeat", 
                             "message": "Recording active", 
                             "capture_method": capture_method,
                             "listeners_working": listeners_working}
            self.events_file.write(json.dumps(heartbeat_event) + "\n")
            self.events_file.flush()

            # Periodically add sentinel events to the events file to ensure it's not empty
            last_sentinel_time = time.time()
            last_file_write_time = time.time()
            events_count = 0
            
            # Main event processing loop
            while self._is_recording:
                current_time = time.time()
                try:
                    # Check if we need to write a sentinel directly to the file
                    # This is our backup to ensure the file is never empty
                    if current_time - last_file_write_time > 5:
                        logger.info("Direct sentinel: ensuring events file has content")
                        direct_sentinel = {
                            "time_stamp": time.perf_counter(), 
                            "action": "direct_sentinel", 
                            "timestamp": current_time
                        }
                        self.events_file.write(json.dumps(direct_sentinel) + "\n")
                        self.events_file.flush()
                        last_file_write_time = current_time
                    
                    # Process events from the queue with a short timeout
                    try:
                        event = self.event_queue.get(timeout=0.5)  # Shorter timeout for more frequent checks
                        self.events_file.write(json.dumps(event) + "\n")
                        self.events_file.flush()  # Flush after each event to ensure it's written
                        events_count += 1
                        last_file_write_time = current_time  # Update last write time
                    except Empty:
                        # No events in the queue, check if we need to add a sentinel
                        if current_time - last_sentinel_time > 2:
                            sentinel_event = {
                                "time_stamp": time.perf_counter(), 
                                "action": "sentinel", 
                                "timestamp": current_time
                            }
                            self.event_queue.put(sentinel_event, block=False)
                            last_sentinel_time = current_time
                    
                    # Log heartbeat periodically
                    if time.time() % 10 < 0.1:
                        if not hasattr(self, '_last_heartbeat') or time.time() - self._last_heartbeat > 10:
                            logger.info(f"Heartbeat: Input monitoring ({capture_method}) is running. Events recorded: {events_count}")
                            self._last_heartbeat = time.time()
                            
                except Exception as e:
                    logger.error(f"Error processing event: {e}")
                    time.sleep(0.1)  # Brief sleep on error
        except Exception as e:
            logger.error(f"Error in recording thread: {e}")
        
        # Add a final event
        end_event = {"time_stamp": time.perf_counter(), "action": "recording_ended"}
        try:
            self.events_file.write(json.dumps(end_event) + "\n")
            self.events_file.flush()
        except Exception as e:
            logger.error(f"Error writing final event: {e}")

    def stop_recording(self):
        if self._is_recording:
            logger.info("Stopping recording...")
            self._is_recording = False

            try:
                # Add a small delay to ensure pending events are processed
                time.sleep(0.5)
                
                # Clean shutdown of event listeners if they exist
                logger.info("Stopping event listeners...")
                try:
                    if system() == "Darwin" and self.use_fallback and hasattr(self, 'macos_monitor'):
                        # Stop the macOS fallback monitor
                        self.macos_monitor.stop()
                    else:
                        if hasattr(self, 'mouse_listener') and self.mouse_listener and self.mouse_listener.running:
                            self.mouse_listener.stop()
                        if hasattr(self, 'keyboard_listener') and self.keyboard_listener and self.keyboard_listener.running:
                            self.keyboard_listener.stop()
                except Exception as e:
                    logger.error(f"Error stopping listeners: {e}")
                
                # Finalize metadata
                logger.info("Finalizing metadata...")
                try:
                    self.metadata_manager.end_collect()
                except Exception as e:
                    logger.error(f"Error finalizing metadata: {e}")
                
                # Stop OBS recording
                logger.info("Stopping OBS recording...")
                try:
                    self.obs_client.stop_recording()
                    self.metadata_manager.add_obs_record_state_timings(self.obs_client.record_state_events)
                except Exception as e:
                    logger.error(f"Error stopping OBS recording: {e}")
                
                # Close and flush events file
                logger.info("Closing events file...")
                try:
                    self.events_file.flush()
                    self.events_file.close()
                except Exception as e:
                    logger.error(f"Error closing events file: {e}")
                
                # Save metadata
                logger.info("Saving metadata...")
                try:
                    self.metadata_manager.save_metadata()
                except Exception as e:
                    logger.error(f"Error saving metadata: {e}")
                
                logger.info(f"Recording stopped and saved to {self.recording_path}")
                self.recording_stopped.emit()
            except Exception as e:
                logger.error(f"Error during recording shutdown: {e}")
                self.recording_stopped.emit()
    
    def pause_recording(self):
        if not self._is_paused and self._is_recording:
            self._is_paused = True
            self.obs_client.pause_recording()
            self.event_queue.put({"time_stamp": time.perf_counter(),
                                  "action": "pause"}, block=False)

    def resume_recording(self):
        if self._is_paused and self._is_recording:
            self._is_paused = False
            self.obs_client.resume_recording()
            self.event_queue.put({"time_stamp": time.perf_counter(),
                                  "action": "resume"}, block=False)

    def _get_recording_path(self) -> str:
        recordings_dir = get_recordings_dir()

        if not os.path.exists(recordings_dir):
            os.mkdir(recordings_dir)

        current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        
        recording_path = os.path.join(recordings_dir, f"recording-{current_time}")
        os.mkdir(recording_path)

        return recording_path