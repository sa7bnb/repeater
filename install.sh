#!/bin/bash

# SA818 Repeater - Automatisk Installation
# Installerar allt som behövs för SA818 Repeater med webbgränssnitt

set -e  # Avsluta vid fel

# Färger för utskrift
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Funktioner för färgad utskrift
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE} SA818 Repeater - Automatisk Installation${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

# Kontrollera om vi kör som root
if [[ $EUID -eq 0 ]]; then
    print_error "Kör INTE detta skript som root eller med sudo!"
    print_info "Kör: curl -sSL URL | bash"
    exit 1
fi

print_header

# Kontrollera att vi är på Raspberry Pi
if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
    print_warning "Detta verkar inte vara en Raspberry Pi"
    read -p "Fortsätt ändå? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Uppdatera systemet
print_info "Uppdaterar systemet..."
sudo apt update && sudo apt upgrade -y

# Installera systempaket
print_info "Installerar systempaket..."
sudo apt install -y \
    python3 python3-pip python3-dev python3-venv \
    python3-pyaudio python3-pygame python3-flask python3-flask-socketio \
    python3-usb python3-serial python3-numpy python3-mutagen \
    python3-eventlet alsa-utils pulseaudio \
    ffmpeg sox lame espeak git usbutils

# Installera Python-paket via pip (backup)
print_info "Installerar Python-paket..."
pip3 install --user --upgrade \
    pyaudio pygame flask flask-socketio \
    pyusb pyserial numpy mutagen eventlet 2>/dev/null || true

# Lägg till användare i grupper
print_info "Konfigurerar användarrättigheter..."
sudo usermod -a -G audio,dialout,plugdev,gpio $USER

# Skapa udev-regel för CM108
print_info "Skapar USB-rättigheter..."
sudo tee /etc/udev/rules.d/99-cm108.rules >/dev/null <<EOF
SUBSYSTEM=="usb", ATTR{idVendor}=="0d8c", ATTR{idProduct}=="0012", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="0d8c", ATTR{idProduct}=="000c", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="0d8c", ATTR{idProduct}=="000e", MODE="0666", GROUP="plugdev"
SUBSYSTEM=="usb", ATTR{idVendor}=="0d8c", ATTR{idProduct}=="013c", MODE="0666", GROUP="plugdev"
EOF

# Tillämpa udev-regler
sudo udevadm control --reload-rules
sudo udevadm trigger

# Skapa repeater-katalog
print_info "Skapar repeater-katalog..."
mkdir -p ~/repeater
cd ~/repeater

# Skapa huvudskriptet
print_info "Skapar repeater-skript..."
cat > repeater.py << 'EOF'
#!/usr/bin/env python3
"""
SA818 Repeater med webbgränssnitt - Automatiskt installerat
"""

import pyaudio
import time
import threading
import logging
import usb.core
import usb.util
import os
import subprocess
from datetime import datetime, timedelta
import json
from flask import Flask, render_template_string, request, jsonify
from flask_socketio import SocketIO, emit
import pygame
import math
import struct
import tempfile
import wave

# Konfigurera logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Tysta Flask/SocketIO loggar
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('socketio').setLevel(logging.ERROR)
logging.getLogger('engineio').setLevel(logging.ERROR)

class CM108Controller:
    """Kontrollerar CM108 HID-funktioner"""
    
    def __init__(self, vendor_id=0x0d8c, product_id=0x0012):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.device = None
        self.endpoint_in = None
        self.hid_intf = None
        self.cos_callback = None
        self.last_cos_state = False
        self.monitoring = False
        self.monitor_thread = None
        self._interface_claimed = False
        
        self.connect_device()
    
    def connect_device(self):
        """Anslut till CM108 USB-enhet"""
        try:
            # Prova olika CM108-varianter
            variants = [
                (0x0d8c, 0x0012),  # Din variant
                (0x0d8c, 0x000c),  # Standard CM108
                (0x0d8c, 0x000e),  # CM108AH
                (0x0d8c, 0x013c),  # CM108B
            ]
            
            for vid, pid in variants:
                self.device = usb.core.find(idVendor=vid, idProduct=pid)
                if self.device:
                    self.vendor_id, self.product_id = vid, pid
                    break
            
            if self.device is None:
                raise Exception("CM108 enhet hittades inte")
            
            cfg = self.device.get_active_configuration()
            
            for intf in cfg:
                if intf.bInterfaceClass == 3:  # HID
                    self.hid_intf = intf
                    break
            
            if self.hid_intf is None:
                raise Exception("HID interface hittades inte")
            
            self.endpoint_in = usb.util.find_descriptor(
                self.hid_intf,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            )
            
            if self.endpoint_in is None:
                raise Exception("HID IN endpoint hittades inte")
            
            logger.info(f"CM108 ansluten - VID:PID {self.vendor_id:04x}:{self.product_id:04x}")
            self._claim_interface()
            
        except Exception as e:
            logger.error(f"Fel vid CM108 anslutning: {e}")
            self.device = None
    
    def _claim_interface(self):
        """Claima HID interface"""
        try:
            subprocess.run(['sudo', 'systemctl', 'stop', 'alsa-state'], 
                         capture_output=True, text=True)
            
            if self.device.is_kernel_driver_active(self.hid_intf.bInterfaceNumber):
                self.device.detach_kernel_driver(self.hid_intf.bInterfaceNumber)
            
            usb.util.claim_interface(self.device, self.hid_intf)
            self._interface_claimed = True
            
            logger.info("CM108 HID interface claimad")
            
        except Exception as e:
            logger.warning(f"Kunde inte claima HID interface: {e}")
            self._interface_claimed = False
    
    def set_ptt(self, active):
        """Aktivera/avaktivera PTT"""
        if not self.device or not self._interface_claimed:
            return False
        
        try:
            gpio_mask = 0x04
            gpio_data = 0x04 if active else 0x00
            data = [0x00, gpio_mask, gpio_data, 0x00]
            
            self.device.ctrl_transfer(0x21, 0x09, 0x0200, 
                                    self.hid_intf.bInterfaceNumber, 
                                    data, timeout=1000)
            return True
        except Exception as e:
            return False
    
    def read_cos(self):
        """Läs COS status"""
        if not self.device or not self.endpoint_in or not self._interface_claimed:
            return False
        
        try:
            data = self.endpoint_in.read(4, timeout=50)
            cos_active = bool(data[0] & 0x02)
            return cos_active
        except usb.core.USBTimeoutError:
            return self.last_cos_state
        except Exception as e:
            return False
    
    def start_monitoring(self, callback):
        """Starta COS monitoring"""
        self.cos_callback = callback
        self.monitoring = True
        
        self.monitor_thread = threading.Thread(target=self._monitor_cos)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()
    
    def _monitor_cos(self):
        """Monitor COS i bakgrunden"""
        while self.monitoring:
            try:
                current_cos = self.read_cos()
                
                if current_cos != self.last_cos_state:
                    self.last_cos_state = current_cos
                    if self.cos_callback:
                        self.cos_callback(current_cos)
                
                time.sleep(0.02)
                
            except Exception as e:
                time.sleep(0.5)
    
    def stop_monitoring(self):
        """Stoppa COS monitoring"""
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1)
        
        if self._interface_claimed:
            try:
                usb.util.release_interface(self.device, self.hid_intf)
            except:
                pass

class SA818Repeater:
    def __init__(self):
        """Initialisera SA818 Repeater"""
        
        # Audio inställningar
        self.audio_format = pyaudio.paInt16
        self.channels = 1
        self.sample_rate = 44100
        self.chunk_size = 512
        
        # Volymkontroll
        self.input_volume = 1.0
        self.output_volume = 1.2
        
        # Identifieringsfunktion
        self.id_enabled = True
        self.id_interval = 600  # 10 minuter
        self.id_file = "station_id.mp3"
        self.last_id_time = datetime.now()
        
        # Status variabler
        self.is_receiving = False
        self.is_transmitting = False
        self.is_playing_id = False
        self.audio_buffer = []
        self.recording = False
        self.pre_buffer = []
        self.pre_buffer_size = 15
        self.pre_recording = False
        
        # Statistik
        self.stats = {
            'total_transmissions': 0,
            'total_receptions': 0,
            'uptime_start': datetime.now(),
            'last_activity': None
        }
        
        # Initialisera komponenter
        self.setup_cm108()
        self.setup_audio()
        self.start_pre_recording()
        self.setup_web_server()
        
        logger.info("SA818 Repeater initialiserad")
    
    def setup_cm108(self):
        """Konfigurera CM108"""
        try:
            self.cm108 = CM108Controller()
            if self.cm108.device:
                self.cm108.set_ptt(False)
                time.sleep(0.1)
                self.cm108.start_monitoring(self.cos_callback)
                logger.info("CM108 startat")
            else:
                logger.error("CM108 kunde inte initialiseras")
                self.cm108 = None
        except Exception as e:
            logger.error(f"Fel vid CM108 setup: {e}")
            self.cm108 = None
    
    def setup_audio(self):
        """Konfigurera audio"""
        self.audio = pyaudio.PyAudio()
        
        self.input_device = None
        self.output_device = None
        
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            device_name = info['name'].lower()
            
            if any(keyword in device_name for keyword in ['usb audio', 'cm108', 'c-media']):
                if info['maxInputChannels'] > 0:
                    self.input_device = i
                if info['maxOutputChannels'] > 0:
                    self.output_device = i
        
        if self.input_device is None:
            self.input_device = 1
            self.output_device = 1
        
        logger.info(f"Audio: {self.sample_rate} Hz, enheter {self.input_device}/{self.output_device}")
    
    def setup_web_server(self):
        """Konfigurera webbserver"""
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = 'sa818_secret'
        
        try:
            self.socketio = SocketIO(self.app, cors_allowed_origins="*", async_mode='threading')
        except:
            self.socketio = None
        
        @self.app.route('/')
        def index():
            return '''
<!DOCTYPE html>
<html>
<head>
    <title>SA818 Repeater</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f0f0f0; }
        .container { max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 10px; }
        h1 { color: #333; text-align: center; }
        .status { padding: 10px; margin: 10px 0; border-radius: 5px; }
        .active { background: #d4edda; color: #155724; }
        .inactive { background: #f8d7da; color: #721c24; }
        .controls { margin: 20px 0; }
        button { padding: 10px 20px; margin: 5px; border: none; border-radius: 5px; cursor: pointer; }
        .btn-primary { background: #007bff; color: white; }
        .btn-success { background: #28a745; color: white; }
        input[type="range"] { width: 100%; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎙️ SA818 Repeater</h1>
        
        <div class="controls">
            <h3>Status</h3>
            <div id="status">Laddar...</div>
        </div>
        
        <div class="controls">
            <h3>Volym</h3>
            <label>Inspelning: <span id="in-vol">1.0</span></label>
            <input type="range" id="input-vol" min="0" max="2" step="0.1" value="1.0">
            
            <label>Utsändning: <span id="out-vol">1.2</span></label>
            <input type="range" id="output-vol" min="0" max="2" step="0.1" value="1.2">
        </div>
        
        <div class="controls">
            <h3>Stations-ID</h3>
            <button class="btn-success" onclick="playId()">Spela ID Nu</button>
            <p>ID-fil: <span id="id-status">Kontrollerar...</span></p>
        </div>
        
        <div class="controls">
            <h3>Statistik</h3>
            <div id="stats">Laddar...</div>
        </div>
    </div>
    
    <script>
        function updateStatus() {
            fetch('/api/status')
                .then(response => response.json())
                .then(data => {
                    document.getElementById('status').innerHTML = 
                        '<div class="status ' + (data.cos_active ? 'active' : 'inactive') + '">COS: ' + (data.cos_active ? 'Aktiv' : 'Inaktiv') + '</div>' +
                        '<div class="status ' + (data.is_receiving ? 'active' : 'inactive') + '">Mottagning: ' + (data.is_receiving ? 'Aktiv' : 'Inaktiv') + '</div>' +
                        '<div class="status ' + (data.is_transmitting ? 'active' : 'inactive') + '">Sändning: ' + (data.is_transmitting ? 'Aktiv' : 'Inaktiv') + '</div>';
                    
                    document.getElementById('in-vol').textContent = data.input_volume.toFixed(1);
                    document.getElementById('out-vol').textContent = data.output_volume.toFixed(1);
                    document.getElementById('input-vol').value = data.input_volume;
                    document.getElementById('output-vol').value = data.output_volume;
                    
                    document.getElementById('id-status').textContent = data.id_file_exists ? 'Hittad' : 'Saknas';
                    
                    document.getElementById('stats').innerHTML = 
                        'Mottagningar: ' + data.stats.total_receptions + '<br>' +
                        'Sändningar: ' + data.stats.total_transmissions + '<br>' +
                        'Drifttid: ' + data.stats.uptime;
                });
        }
        
        function playId() {
            fetch('/api/id', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ trigger: true })
            });
        }
        
        document.getElementById('input-vol').addEventListener('input', function() {
            fetch('/api/volume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ input: parseFloat(this.value) })
            });
        });
        
        document.getElementById('output-vol').addEventListener('input', function() {
            fetch('/api/volume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ output: parseFloat(this.value) })
            });
        });
        
        setInterval(updateStatus, 2000);
        updateStatus();
    </script>
</body>
</html>
            '''
        
        @self.app.route('/api/status')
        def api_status():
            return jsonify(self.get_web_status())
        
        @self.app.route('/api/volume', methods=['POST'])
        def api_volume():
            data = request.json
            if 'input' in data:
                self.input_volume = max(0.0, min(2.0, data['input']))
            if 'output' in data:
                self.output_volume = max(0.0, min(2.0, data['output']))
            return jsonify({'status': 'ok'})
        
        @self.app.route('/api/id', methods=['POST'])
        def api_id():
            data = request.json
            if 'trigger' in data and data['trigger']:
                self.play_station_id(manual=True)
            return jsonify({'status': 'ok'})
        
        self.web_thread = threading.Thread(target=self.run_web_server)
        self.web_thread.daemon = True
        self.web_thread.start()
    
    def run_web_server(self):
        """Kör webbserver"""
        try:
            self.app.run(host='0.0.0.0', port=5000, debug=False)
        except Exception as e:
            logger.error(f"Webbserver fel: {e}")
    
    def adjust_volume(self, audio_data_bytes, volume_level):
        """Justera volym"""
        try:
            import array
            audio_data = array.array('h', audio_data_bytes)
            
            for i in range(len(audio_data)):
                sample = int(audio_data[i] * volume_level)
                audio_data[i] = max(-32768, min(32767, sample))
            
            return audio_data.tobytes()
        except Exception as e:
            return audio_data_bytes
    
    def start_pre_recording(self):
        """Starta förregistrering"""
        self.pre_recording = True
        
        pre_record_thread = threading.Thread(target=self.pre_record_audio)
        pre_record_thread.daemon = True
        pre_record_thread.start()
    
    def pre_record_audio(self):
        """Förregistrering"""
        try:
            stream = self.audio.open(
                format=self.audio_format,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=self.input_device,
                frames_per_buffer=self.chunk_size
            )
            
            while self.pre_recording:
                try:
                    data = stream.read(self.chunk_size, exception_on_overflow=False)
                    adjusted_data = self.adjust_volume(data, self.input_volume)
                    
                    self.pre_buffer.append(adjusted_data)
                    
                    if len(self.pre_buffer) > self.pre_buffer_size:
                        self.pre_buffer.pop(0)
                    
                    if self.recording:
                        self.audio_buffer.append(adjusted_data)
                    
                    time.sleep(0.001)
                    
                except Exception as e:
                    time.sleep(0.1)
                    continue
            
            stream.stop_stream()
            stream.close()
            
        except Exception as e:
            logger.error(f"Förregistrering fel: {e}")
    
    def cos_callback(self, cos_active):
        """COS callback"""
        if cos_active:
            if not self.is_receiving and not self.is_transmitting and not self.is_playing_id:
                self.start_recording()
        else:
            if self.is_receiving:
                self.stop_recording()
                threading.Timer(0.1, self.start_playback).start()
    
    def start_recording(self):
        """Starta inspelning"""
        self.is_receiving = True
        self.recording = True
        self.audio_buffer = self.pre_buffer.copy()
        self.stats['total_receptions'] += 1
        self.stats['last_activity'] = datetime.now()
        
        self.record_thread = threading.Thread(target=self.record_audio)
        self.record_thread.daemon = True
        self.record_thread.start()
    
    def stop_recording(self):
        """Stoppa inspelning"""
        self.recording = False
        self.is_receiving = False
    
    def record_audio(self):
        """Inspelning"""
        try:
            while self.recording:
                time.sleep(0.1)
        except Exception as e:
            logger.error(f"Inspelning fel: {e}")
    
    def start_playback(self):
        """Starta uppspelning"""
        if not self.audio_buffer or self.is_receiving or self.is_playing_id:
            return
        
        self.is_transmitting = True
        self.stats['total_transmissions'] += 1
        
        if self.cm108:
            self.cm108.set_ptt(True)
        
        time.sleep(0.1)
        
        playback_thread = threading.Thread(target=self.playback_audio)
        playback_thread.daemon = True
        playback_thread.start()
    
    def playback_audio(self):
        """Uppspelning"""
        try:
            stream = self.audio.open(
                format=self.audio_format,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                output_device_index=self.output_device,
                frames_per_buffer=self.chunk_size
            )
            
            silence = b'\x00' * self.chunk_size
            stream.write(silence)
            
            for chunk in self.audio_buffer:
                adjusted_chunk = self.adjust_volume(chunk, self.output_volume)
                stream.write(adjusted_chunk)
            
            stream.write(silence)
            
            stream.stop_stream()
            stream.close()
            
            time.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Uppspelning fel: {e}")
        finally:
            if self.cm108:
                self.cm108.set_ptt(False)
            self.is_transmitting = False
    
    def play_station_id(self, manual=False):
        """Spela stations-ID"""
        if self.is_receiving or self.is_transmitting or self.is_playing_id:
            return
        
        self.is_playing_id = True
        self.last_id_time = datetime.now()
        
        id_thread = threading.Thread(target=self._play_id_audio)
        id_thread.daemon = True
        id_thread.start()
        
        if manual:
            logger.info("Manuell ID-uppspelning")
    
    def _play_id_audio(self):
        """ID-uppspelning"""
        try:
            if self.cm108:
                self.cm108.set_ptt(True)
            
            time.sleep(0.1)
            
            # Spela enkel ton som ID
            duration = 2.0
            frequency = 800
            
            stream = self.audio.open(
                format=self.audio_format,
                channels=1,
                rate=self.sample_rate,
                output=True,
                output_device_index=self.output_device,
                frames_per_buffer=self.chunk_size
            )
            
            samples = int(duration * self.sample_rate)
            
            for i in range(0, samples, self.chunk_size):
                chunk_samples = min(self.chunk_size, samples - i)
                chunk_data = []
                
                for j in range(chunk_samples):
                    sample_index = i + j
                    value = int(8192 * math.sin(2 * math.pi * frequency * sample_index / self.sample_rate))
                    chunk_data.append(value)
                
                chunk_bytes = struct.pack('<' + 'h' * len(chunk_data), *chunk_data)
                stream.write(chunk_bytes)
            
            stream.stop_stream()
            stream.close()
            
            logger.info("ID-ton spelad")
            
        except Exception as e:
            logger.error(f"ID-uppspelning fel: {e}")
        finally:
            if self.cm108:
                self.cm108.set_ptt(False)
            self.is_playing_id = False
    
    def get_web_status(self):
        """Hämta status"""
        uptime = datetime.now() - self.stats['uptime_start']
        
        return {
            'cos_active': self.cm108.last_cos_state if self.cm108 else False,
            'is_receiving': self.is_receiving,
            'is_transmitting': self.is_transmitting,
            'is_playing_id': self.is_playing_id,
            'input_volume': self.input_volume,
            'output_volume': self.output_volume,
            'id_file_exists': os.path.exists(self.id_file),
            'stats': {
                'total_transmissions': self.stats['total_transmissions'],
                'total_receptions': self.stats['total_receptions'],
                'uptime': str(uptime).split('.')[0],
            }
        }
    
    def run(self):
        """Huvudloop"""
        logger.info("SA818 Repeater startad")
        logger.info("Webbgränssnitt: http://localhost:5000")
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Avslutar...")
            self.cleanup()
    
    def cleanup(self):
        """Cleanup"""
        self.pre_recording = False
        
        if self.cm108:
            self.cm108.set_ptt(False)
            self.cm108.stop_monitoring()
        
        self.audio.terminate()
        
        logger.info("Cleanup klar")

def main():
    try:
        repeater = SA818Repeater()
        repeater.run()
    except Exception as e:
        logger.error(f"Fel vid start: {e}")

if __name__ == "__main__":
    main()
EOF

# Gör skriptet körbart
chmod +x repeater.py

# Skapa enkel ID-fil
print_info "Skapar ID-fil..."
if command -v espeak >/dev/null 2>&1; then
    espeak "Repeater online" -w station_id.wav -s 120 2>/dev/null
    if command -v lame >/dev/null 2>&1; then
        lame station_id.wav station_id.mp3 2>/dev/null
        rm -f station_id.wav
    fi
fi

# Skapa systemd-tjänst
print_info "Skapar systemd-tjänst..."
sudo tee /etc/systemd/system/sa818-repeater.service >/dev/null <<EOF
[Unit]
Description=SA818 Repeater med webbgränssnitt
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/repeater
ExecStart=/usr/bin/python3 $HOME/repeater/repeater.py
Restart=always
RestartSec=10
Environment=PYTHONPATH=/usr/local/lib/python3.11/dist-packages

[Install]
WantedBy=multi-user.target
EOF

# Aktivera tjänsten
sudo systemctl daemon-reload
sudo systemctl enable sa818-repeater.service

# Kontrollera installation
print_info "Kontrollerar installation..."

# Testa Python-paket
if python3 -c "import pyaudio, flask, usb.core; print('Python-paket OK')" 2>/dev/null; then
    print_success "Python-paket installerade"
else
    print_warning "Några Python-paket saknas"
fi

# Kontrollera CM108
if lsusb | grep -i "c-media\|0d8c" >/dev/null; then
    print_success "CM108 hittad"
else
    print_warning "CM108 inte ansluten"
fi

# Kontrollera ljudenheter
if aplay -l | grep -i "usb" >/dev/null 2>&1; then
    print_success "USB-ljud hittad"
else
    print_warning "Ingen USB-ljudenhet hittad"
fi

# Installationssammanfattning
print_success "Installation klar!"
echo
print_info "Starta repeatern manuellt:"
echo "  cd ~/repeater"
echo "  sudo python3 repeater.py"
echo
print_info "Starta som systemtjänst:"
echo "  sudo systemctl start sa818-repeater"
echo "  sudo systemctl status sa818-repeater"
echo
print_info "Webbgränssnitt:"
echo "  http://localhost:5000"
echo "  http://$(hostname -I | cut -d' ' -f1):5000"
echo
print_info "Loggar om du kör som tjänst:"
echo "  sudo journalctl -u sa818-repeater -f"
echo
print_warning "VIKTIGT: Logga ut och in igen för att grupprättigheter ska träda i kraft!"
print_warning "Eller kör: newgrp audio && newgrp dialout && newgrp plugdev"
echo

# Fråga om vi ska starta direkt
read -p "Vill du starta repeatern nu? (y/N): " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    print_info "Startar repeater..."
    cd ~/repeater
    
    # Kontrollera att gruppmedlemskap är aktivt
    if groups | grep -q "audio.*dialout.*plugdev\|plugdev.*audio.*dialout\|dialout.*plugdev.*audio"; then
        print_info "Startar som användare..."
        exec python3 repeater.py
    else
        print_info "Startar med sudo (grupprättigheter inte aktiva än)..."
        exec sudo python3 repeater.py
    fi
else
    print_info "Kör senare med: cd ~/repeater && sudo python3 repeater.py"
fi
