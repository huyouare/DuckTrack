import os
import sys
import logging
from platform import system
import time
from datetime import datetime

from PyQt6.QtCore import QTimer, pyqtSlot, Qt
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog,
                             QFormLayout, QLabel, QLineEdit, QMenu,
                             QMessageBox, QPushButton, QSystemTrayIcon,
                             QTextEdit, QVBoxLayout, QWidget)

# Import using absolute paths for PyInstaller compatibility
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    # Running in a PyInstaller bundle
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ducktrack.obs_client import close_obs, is_obs_running, open_obs
    from ducktrack.playback import Player, get_latest_recording
    from ducktrack.recorder import Recorder
    from ducktrack.util import get_recordings_dir, open_file
else:
    # Running in a normal Python environment
    from .obs_client import close_obs, is_obs_running, open_obs
    from .playback import Player, get_latest_recording
    from .recorder import Recorder
    from .util import get_recordings_dir, open_file

# Set up logging to file
log_file = os.path.expanduser("~/ducktrack.log")
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename=log_file,
    filemode='a'
)

# Also log to stderr
console = logging.StreamHandler()
console.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console.setFormatter(formatter)
logging.getLogger('').addHandler(console)

logger = logging.getLogger('DuckTrack')

# Redirect print statements to logger
original_print = print
def print_to_log(*args, **kwargs):
    message = ' '.join(str(arg) for arg in args)
    logger.info(message)
    original_print(*args, **kwargs)
print = print_to_log

logger.info("----- DuckTrack Starting -----")

class TitleDescriptionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Recording Details")
        self.setMinimumWidth(400)  # Make the dialog a bit wider
        
        layout = QVBoxLayout(self)

        self.form_layout = QFormLayout()

        self.title_label = QLabel("Title:")
        self.title_input = QLineEdit(self)
        self.title_input.setText(f"Recording-{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}")  # Default title
        self.form_layout.addRow(self.title_label, self.title_input)

        self.description_label = QLabel("Description:")
        self.description_input = QTextEdit(self)
        self.description_input.setPlaceholderText("Enter a description of what happened in this recording...")
        self.description_input.setMinimumHeight(100)  # Make the description box taller
        self.form_layout.addRow(self.description_label, self.description_input)

        layout.addLayout(self.form_layout)

        self.submit_button = QPushButton("Save", self)
        self.submit_button.clicked.connect(self.accept)
        layout.addWidget(self.submit_button)

    def get_values(self):
        return self.title_input.text(), self.description_input.toPlainText()

class MainInterface(QWidget):
    def __init__(self, app: QApplication):
        super().__init__()
        self.tray = QSystemTrayIcon(QIcon(resource_path("assets/duck.png")))
        self.tray.show()
                
        self.app = app
        self.obs_process = None
        
        self.init_tray()
        self.init_window()
        
        # Check if on macOS and ensure permissions
        if system() == "Darwin":
            self.check_macos_permissions()
        
        # Check if OBS is already running before attempting to start it
        self.ensure_obs_running()

    def check_macos_permissions(self):
        """Guide the user through setting up required permissions on macOS."""
        # Inform the user about required permissions
        QMessageBox.information(
            self, 
            "Permissions Required",
            "DuckTrack needs several permissions to function correctly:\n\n"
            "1. Screen Recording - to capture your screen\n"
            "2. Accessibility - to track mouse movements\n"
            "3. Input Monitoring - to track keyboard events\n\n"
            "You may be prompted to allow these permissions. Please grant them when asked."
        )
        
        # For debugging, try to trigger permission checks
        import subprocess
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to keystroke ""'],
                capture_output=True, timeout=1
            )
        except:
            pass

    def ensure_obs_running(self):
        """Make sure OBS is running, launching it if necessary."""
        if not is_obs_running():
            try:
                print("Starting OBS...")
                self.obs_process = open_obs()
                # Wait a moment for OBS to initialize
                time.sleep(2)
            except Exception as e:
                self.display_error_message(f"Failed to start OBS: {str(e)}\nPlease start OBS manually.")
        else:
            print("OBS is already running")

    def init_window(self):
        self.setWindowTitle("DuckTrack")
        layout = QVBoxLayout(self)
        
        self.toggle_record_button = QPushButton("Start Recording", self)
        self.toggle_record_button.clicked.connect(self.toggle_record)
        layout.addWidget(self.toggle_record_button)
        
        self.toggle_pause_button = QPushButton("Pause Recording", self)
        self.toggle_pause_button.clicked.connect(self.toggle_pause)
        self.toggle_pause_button.setEnabled(False)
        layout.addWidget(self.toggle_pause_button)
        
        self.show_recordings_button = QPushButton("Show Recordings", self)
        self.show_recordings_button.clicked.connect(lambda: open_file(get_recordings_dir()))
        layout.addWidget(self.show_recordings_button)
        
        self.play_latest_button = QPushButton("Play Latest Recording", self)
        self.play_latest_button.clicked.connect(self.play_latest_recording)
        layout.addWidget(self.play_latest_button)
        
        self.play_custom_button = QPushButton("Play Custom Recording", self)
        self.play_custom_button.clicked.connect(self.play_custom_recording)
        layout.addWidget(self.play_custom_button)
        
        self.replay_recording_button = QPushButton("Replay Recording", self)
        self.replay_recording_button.clicked.connect(self.replay_recording)
        self.replay_recording_button.setEnabled(False)
        layout.addWidget(self.replay_recording_button)
        
        # Add a view logs button
        self.view_logs_button = QPushButton("View Debug Logs", self)
        self.view_logs_button.clicked.connect(self.show_log_viewer)
        layout.addWidget(self.view_logs_button)
        
        self.quit_button = QPushButton("Quit", self)
        self.quit_button.clicked.connect(self.quit)
        layout.addWidget(self.quit_button)
        
        self.natural_scrolling_checkbox = QCheckBox("Natural Scrolling", self, checked=system() == "Darwin")
        layout.addWidget(self.natural_scrolling_checkbox)

        self.natural_scrolling_checkbox.stateChanged.connect(self.toggle_natural_scrolling)
        
        self.setLayout(layout)
        
    def init_tray(self):
        self.menu = QMenu()
        self.tray.setContextMenu(self.menu)

        self.toggle_record_action = QAction("Start Recording")
        self.toggle_record_action.triggered.connect(self.toggle_record)
        self.menu.addAction(self.toggle_record_action)

        self.toggle_pause_action = QAction("Pause Recording")
        self.toggle_pause_action.triggered.connect(self.toggle_pause)
        self.toggle_pause_action.setVisible(False)
        self.menu.addAction(self.toggle_pause_action)
        
        self.show_recordings_action = QAction("Show Recordings")
        self.show_recordings_action.triggered.connect(lambda: open_file(get_recordings_dir()))
        self.menu.addAction(self.show_recordings_action)
        
        self.play_latest_action = QAction("Play Latest Recording")
        self.play_latest_action.triggered.connect(self.play_latest_recording)
        self.menu.addAction(self.play_latest_action)

        self.play_custom_action = QAction("Play Custom Recording")
        self.play_custom_action.triggered.connect(self.play_custom_recording)
        self.menu.addAction(self.play_custom_action)
        
        self.replay_recording_action = QAction("Replay Recording")
        self.replay_recording_action.triggered.connect(self.replay_recording)
        self.menu.addAction(self.replay_recording_action)
        self.replay_recording_action.setVisible(False)

        self.quit_action = QAction("Quit")
        self.quit_action.triggered.connect(self.quit)
        self.menu.addAction(self.quit_action)
        
        self.menu.addSeparator()
        
        self.natural_scrolling_option = QAction("Natural Scrolling", checkable=True, checked=system() == "Darwin")
        self.natural_scrolling_option.triggered.connect(self.toggle_natural_scrolling)
        self.menu.addAction(self.natural_scrolling_option)
        
    @pyqtSlot()
    def replay_recording(self):
        player = Player()
        if hasattr(self, "last_played_recording_path"):
            player.play(self.last_played_recording_path)
        else:
            self.display_error_message("No recording has been played yet!")

    @pyqtSlot()
    def play_latest_recording(self):
        player = Player()
        recording_path = get_latest_recording()
        self.last_played_recording_path = recording_path
        self.replay_recording_action.setVisible(True)
        self.replay_recording_button.setEnabled(True)
        player.play(recording_path)

    @pyqtSlot()
    def play_custom_recording(self):
        player = Player()
        directory = QFileDialog.getExistingDirectory(None, "Select Recording", get_recordings_dir())
        if directory:
            self.last_played_recording_path = directory
            self.replay_recording_button.setEnabled(True)
            self.replay_recording_action.setVisible(True)
            player.play(directory)

    @pyqtSlot()
    def quit(self):
        print("Shutting down DuckTrack...")
        
        # First stop any active recording
        if hasattr(self, "recorder_thread"):
            print("Stopping active recording...")
            try:
                self.recorder_thread.stop_recording()
                self.recorder_thread.terminate()
                del self.recorder_thread
            except Exception as e:
                print(f"Error stopping recording: {e}")
        
        # Only close OBS if we started it
        if hasattr(self, "obs_process") and self.obs_process:
            print("Closing OBS...")
            try:
                close_obs(self.obs_process)
                self.obs_process = None
            except Exception as e:
                print(f"Error closing OBS: {e}")
        
        print("Quitting application...")
        self.app.quit()

    def closeEvent(self, event):
        self.quit()

    @pyqtSlot()
    def toggle_natural_scrolling(self):
        sender = self.sender()

        if sender == self.natural_scrolling_checkbox:
            state = self.natural_scrolling_checkbox.isChecked()
            self.natural_scrolling_option.setChecked(state)
        else:
            state = self.natural_scrolling_option.isChecked()
            self.natural_scrolling_checkbox.setChecked(state)

    @pyqtSlot()
    def toggle_pause(self):
        if self.recorder_thread._is_paused:
            self.recorder_thread.resume_recording()
            self.toggle_pause_action.setText("Pause Recording")
            self.toggle_pause_button.setText("Pause Recording")
        else:
            self.recorder_thread.pause_recording()
            self.toggle_pause_action.setText("Resume Recording")
            self.toggle_pause_button.setText("Resume Recording")

    @pyqtSlot()
    def toggle_record(self):
        if not hasattr(self, "recorder_thread"):
            # Make sure OBS is running before starting a recording
            if not is_obs_running():
                try:
                    print("OBS not running, attempting to start it...")
                    self.ensure_obs_running()
                    # Wait a bit for OBS to initialize
                    time.sleep(3)
                    if not is_obs_running():
                        self.display_error_message("Failed to start OBS. Please start OBS manually and try again.")
                        return
                except Exception as e:
                    self.display_error_message(f"Error starting OBS: {str(e)}\nPlease start OBS manually.")
                    return
            
            try:
                self.recorder_thread = Recorder(natural_scrolling=self.natural_scrolling_checkbox.isChecked())
                self.recorder_thread.recording_stopped.connect(self.on_recording_stopped)
                self.recorder_thread.start()
                self.update_menu(True)
            except Exception as e:
                self.display_error_message(f"Error starting recording: {str(e)}")
        else:
            try:
                self.recorder_thread.stop_recording()
                self.recorder_thread.terminate()

                recording_dir = self.recorder_thread.recording_path

                del self.recorder_thread
                
                # Show dialog to get title and description
                dialog = TitleDescriptionDialog(self)  # Pass self as parent
                dialog.setWindowModality(Qt.WindowModality.ApplicationModal)  # Make sure it blocks until user responds
                
                # Use QTimer.singleShot to make sure dialog appears on top
                QTimer.singleShot(0, dialog.raise_)
                QTimer.singleShot(0, dialog.activateWindow)
                
                result = dialog.exec()

                if result == QDialog.DialogCode.Accepted:
                    title, description = dialog.get_values()

                    # Use default title if empty
                    if not title:
                        title = f"Recording-{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}"
                    
                    # Rename the directory with the title
                    renamed_dir = os.path.join(os.path.dirname(recording_dir), title)
                    try:
                        os.rename(recording_dir, renamed_dir)
                        print(f"Renamed recording directory to {renamed_dir}")
                    except Exception as e:
                        print(f"Error renaming directory: {e}")
                        renamed_dir = recording_dir  # Fallback to original directory
                    
                    # Create README with the description (even if empty)
                    readme_path = os.path.join(renamed_dir, 'README.md')
                    try:
                        with open(readme_path, 'w') as f:
                            f.write(description or "No description provided.")
                        print(f"Saved README to {readme_path}")
                    except Exception as e:
                        print(f"Error writing README: {e}")
                else:
                    # User canceled - still create a default README
                    try:
                        with open(os.path.join(recording_dir, 'README.md'), 'w') as f:
                            f.write("Recording saved without description.")
                        print("Created default README.md")
                    except Exception as e:
                        print(f"Error creating default README: {e}")
                    
                self.on_recording_stopped()
            except Exception as e:
                self.display_error_message(f"Error stopping recording: {str(e)}")
                self.on_recording_stopped()

    @pyqtSlot()
    def on_recording_stopped(self):
        self.update_menu(False)

    def update_menu(self, is_recording: bool):
        self.toggle_record_button.setText("Stop Recording" if is_recording else "Start Recording")
        self.toggle_record_action.setText("Stop Recording" if is_recording else "Start Recording")
        
        self.toggle_pause_button.setEnabled(is_recording)
        self.toggle_pause_action.setVisible(is_recording)

    def display_error_message(self, message):
        QMessageBox.critical(None, "Error", message)
        
    def show_log_viewer(self):
        """Show a dialog with the contents of the log file."""
        log_file = os.path.expanduser("~/ducktrack.log")
        
        dialog = QDialog(self)
        dialog.setWindowTitle("DuckTrack Debug Logs")
        dialog.resize(800, 600)
        
        layout = QVBoxLayout()
        
        log_text = QTextEdit()
        log_text.setReadOnly(True)
        
        # Load log file contents
        try:
            with open(log_file, 'r') as f:
                log_content = f.read()
                log_text.setText(log_content)
                
                # Scroll to the end
                cursor = log_text.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                log_text.setTextCursor(cursor)
        except Exception as e:
            log_text.setText(f"Error loading log file: {str(e)}")
        
        layout.addWidget(log_text)
        
        # Add refresh button
        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(lambda: self.refresh_log_view(log_text))
        layout.addWidget(refresh_button)
        
        dialog.setLayout(layout)
        dialog.exec()
    
    def refresh_log_view(self, text_edit):
        """Refresh the log view with the latest content."""
        log_file = os.path.expanduser("~/ducktrack.log")
        try:
            with open(log_file, 'r') as f:
                log_content = f.read()
                text_edit.setText(log_content)
                
                # Scroll to the end
                cursor = text_edit.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                text_edit.setTextCursor(cursor)
        except Exception as e:
            text_edit.setText(f"Error loading log file: {str(e)}")

def resource_path(relative_path: str) -> str:
    if hasattr(sys, '_MEIPASS'):
        base_path = getattr(sys, "_MEIPASS")
    else:
        base_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')

    return os.path.join(base_path, relative_path)