#!/usr/bin/env python3
"""
Simplex Repeater (Papegojrepeater) för Jumbospot SHARI SA818
Med webbgränssnitt och identifieringsfunktion
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
import mutagen
from mutagen.mp3 import MP3

# Konfigurera logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Tysta Flask/SocketIO loggar
logging.getLogger('werkzeug').setLevel(logging.ERROR)
logging.getLogger('socketio').setLevel(logging.ERROR)
logging.getLogger('engineio').setLevel(logging.ERROR)

class CM108Controller:
    """Kontrollerar CM108 HID-funktioner för COS-detektion och PTT-kontroll"""
    
    def __init__(self, vendor_id=0x0d8c, product_id=0x0012):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.device = None
        self.endpoint_in = None
        self.endpoint_out = None
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
            self.device = usb.core.find(idVendor=self.vendor_id, idProduct=self.product_id)
            
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
            
            self.endpoint_out = usb.util.find_descriptor(
                self.hid_intf,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            )
            
            if self.endpoint_in is None:
                raise Exception("HID IN endpoint hittades inte")
            
            logger.info(f"CM108 ansluten - VID:PID {self.vendor_id:04x}:{self.product_id:04x}")
            self._claim_interface()
            
        except Exception as e:
            logger.error(f"Fel vid CM108 anslutning: {e}")
            self.device = None
    
    def _claim_interface(self):
        """Claima HID interface för CM108"""
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
        """Aktivera/avaktivera PTT via CM108 GPIO2 (pin 13)"""
        if not self.device or not self._interface_claimed:
            return False
        
        try:
            gpio_mask = 0x04
            gpio_data = 0x04 if active else 0x00
            data = [0x00, gpio_mask, gpio_data, 0x00]
            
            try:
                self.device.ctrl_transfer(0x21, 0x09, 0x0200, 
                                        self.hid_intf.bInterfaceNumber, 
                                        data, timeout=1000)
                return True
            except Exception as e:
                logger.debug(f"PTT fel: {e}")
                return False
                
        except Exception as e:
            logger.error(f"Fel vid PTT kontroll: {e}")
            return False
    
    def read_cos(self):
        """Läs COS status från CM108"""
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
        """Monitor COS status i bakgrunden"""
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
                subprocess.run(['sudo', 'systemctl', 'start', 'alsa-state'], 
                             capture_output=True, text=True)
            except:
                pass

class SA818Repeater:
    def __init__(self):
        """Initialisera SA818 Repeater med webbgränssnitt"""
        
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
        self.id_interval = 600  # 10 minuter default
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
        
        # Initialisera pygame för MP3-uppspelning (bara för att ladda filer)
        pygame.mixer.init()
        
        # Initialisera komponenter
        self.setup_cm108()
        self.setup_audio()
        self.start_pre_recording()
        
        # Starta webbserver
        self.setup_web_server()
        
        logger.info("SA818 Repeater med webbgränssnitt initialiserad")
    
    def setup_cm108(self):
        """Konfigurera CM108 för COS och PTT"""
        try:
            self.cm108 = CM108Controller()
            if self.cm108.device:
                self.cm108.set_ptt(False)
                time.sleep(0.1)
                self.cm108.start_monitoring(self.cos_callback)
                logger.info("CM108 COS monitoring och PTT kontroll startat")
            else:
                logger.error("CM108 kunde inte initialiseras")
                self.cm108 = None
        except Exception as e:
            logger.error(f"Fel vid CM108 setup: {e}")
            self.cm108 = None
    
    def setup_audio(self):
        """Konfigurera audio system för CM108"""
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
        
        if self.input_device is None or self.output_device is None:
            self.input_device = 1
            self.output_device = 1
        
        logger.info(f"Audio config: {self.sample_rate} Hz, enheter {self.input_device}/{self.output_device}")
    
    def setup_web_server(self):
        """Konfigurera Flask webbserver"""
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = 'sa818_repeater_secret'
        
        # Försök med eventlet för WebSocket
        try:
            self.socketio = SocketIO(self.app, cors_allowed_origins="*", async_mode='eventlet')
        except:
            # Fallback utan WebSocket
            self.socketio = SocketIO(self.app, cors_allowed_origins="*", async_mode='threading')
        
        @self.app.route('/')
        def index():
            return render_template_string(HTML_TEMPLATE)
        
        @self.app.route('/api/status')
        def api_status():
            return jsonify(self.get_web_status())
        
        @self.app.route('/api/volume', methods=['POST'])
        def api_volume():
            data = request.json
            if 'input' in data:
                self.set_input_volume(data['input'])
            if 'output' in data:
                self.set_output_volume(data['output'])
            return jsonify({'status': 'ok'})
        
        @self.app.route('/api/id', methods=['POST'])
        def api_id():
            data = request.json
            if 'enabled' in data:
                self.id_enabled = data['enabled']
            if 'interval' in data:
                self.id_interval = data['interval']
            if 'trigger' in data and data['trigger']:
                self.play_station_id(manual=True)
            return jsonify({'status': 'ok'})
        
        @self.socketio.on('connect')
        def handle_connect():
            emit('status', self.get_web_status())
        
        # Starta webbserver i bakgrunden
        self.web_thread = threading.Thread(target=self.run_web_server)
        self.web_thread.daemon = True
        self.web_thread.start()
    
    def run_web_server(self):
        """Kör webbserver"""
        self.socketio.run(self.app, host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    
    def adjust_volume(self, audio_data_bytes, volume_level):
        """Justera volym för audio data"""
        try:
            import array
            audio_data = array.array('h', audio_data_bytes)
            
            for i in range(len(audio_data)):
                sample = int(audio_data[i] * volume_level)
                audio_data[i] = max(-32768, min(32767, sample))
            
            return audio_data.tobytes()
        except Exception as e:
            return audio_data_bytes
    
    def set_input_volume(self, volume):
        """Sätt inspelningsvolym"""
        volume = max(0.0, min(2.0, volume))
        self.input_volume = volume
        self.broadcast_status()
    
    def set_output_volume(self, volume):
        """Sätt utsändningsvolym"""
        volume = max(0.0, min(2.0, volume))
        self.output_volume = volume
        self.broadcast_status()
    
    def start_pre_recording(self):
        """Starta kontinuerlig förregistrering"""
        self.pre_recording = True
        
        pre_record_thread = threading.Thread(target=self.pre_record_audio)
        pre_record_thread.daemon = True
        pre_record_thread.start()
    
    def pre_record_audio(self):
        """Kontinuerlig förregistrering i bakgrunden"""
        try:
            if isinstance(self.input_device, int):
                input_device = self.input_device
            else:
                input_device = None
            
            stream = self.audio.open(
                format=self.audio_format,
                channels=self.channels,
                rate=self.sample_rate,
                input=True,
                input_device_index=input_device,
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
            logger.error(f"Fel vid pre-recording: {e}")
    
    def cos_callback(self, cos_active):
        """Callback för COS signal"""
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
        
        self.broadcast_status()
    
    def stop_recording(self):
        """Stoppa inspelning"""
        self.recording = False
        self.is_receiving = False
        self.broadcast_status()
    
    def record_audio(self):
        """Spela in audio - använder pre-recording stream"""
        try:
            time.sleep(0.05)
            while self.recording:
                time.sleep(0.1)
        except Exception as e:
            logger.error(f"Fel vid inspelning: {e}")
    
    def start_playback(self):
        """Starta återutsändning"""
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
        
        self.broadcast_status()
    
    def playback_audio(self):
        """Spela upp inspelat meddelande"""
        try:
            if isinstance(self.output_device, int):
                output_device = self.output_device
            else:
                output_device = None
            
            stream = self.audio.open(
                format=self.audio_format,
                channels=self.channels,
                rate=self.sample_rate,
                output=True,
                output_device_index=output_device,
                frames_per_buffer=self.chunk_size
            )
            
            silence = b'\x00' * (self.chunk_size * 2)
            stream.write(silence)
            
            for chunk in self.audio_buffer:
                adjusted_chunk = self.adjust_volume(chunk, self.output_volume)
                stream.write(adjusted_chunk)
            
            stream.write(silence)
            
            stream.stop_stream()
            stream.close()
            
            time.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Fel vid uppspelning: {e}")
        finally:
            if self.cm108:
                self.cm108.set_ptt(False)
            self.is_transmitting = False
            self.broadcast_status()
    
    def play_station_id(self, manual=False):
        """Spela stations-ID"""
        if not os.path.exists(self.id_file):
            logger.warning(f"ID-fil {self.id_file} hittades inte")
            return
        
        if self.is_receiving or self.is_transmitting:
            return
        
        self.is_playing_id = True
        self.last_id_time = datetime.now()
        
        id_thread = threading.Thread(target=self._play_id_audio)
        id_thread.daemon = True
        id_thread.start()
        
        self.broadcast_status()
        
        if manual:
            logger.info("Manuell ID-uppspelning startad")
        else:
            logger.info("Automatisk ID-uppspelning startad")
    
    def load_mp3_as_audio(self, filepath):
        """Ladda MP3-fil och konvertera till audio data för CM108"""
        try:
            # Använd pygame för att ladda MP3
            pygame.mixer.init()
            sound = pygame.mixer.Sound(filepath)
            
            # Få raw audio data
            raw_data = pygame.sndarray.array(sound)
            
            # Konvertera till rätt format för CM108
            import numpy as np
            
            # Säkerställ att vi har mono
            if len(raw_data.shape) > 1:
                raw_data = np.mean(raw_data, axis=1)
            
            # Konvertera till 16-bit signed int
            if raw_data.dtype != np.int16:
                raw_data = raw_data.astype(np.int16)
            
            # Resampla till rätt sample rate om nödvändigt
            if sound.get_length() > 0:
                # Uppskatta original sample rate
                original_samples = len(raw_data)
                duration = sound.get_length()  # i sekunder
                original_rate = int(original_samples / duration)
                
                if original_rate != self.sample_rate:
                    # Enkel resampling
                    ratio = self.sample_rate / original_rate
                    new_length = int(len(raw_data) * ratio)
                    indices = np.linspace(0, len(raw_data) - 1, new_length)
                    raw_data = np.interp(indices, np.arange(len(raw_data)), raw_data).astype(np.int16)
            
            # Konvertera till bytes
            audio_bytes = raw_data.tobytes()
            
            # Dela upp i chunks
            chunk_size = self.chunk_size * 2  # 2 bytes per sample
            chunks = []
            for i in range(0, len(audio_bytes), chunk_size):
                chunk = audio_bytes[i:i + chunk_size]
                # Fyll upp sista chunk med tystnad om nödvändigt
                if len(chunk) < chunk_size:
                    chunk += b'\x00' * (chunk_size - len(chunk))
                chunks.append(chunk)
            
            logger.info(f"MP3-fil laddad: {len(chunks)} chunks, {len(audio_bytes)} bytes")
            return chunks
            
        except Exception as e:
            logger.error(f"Fel vid MP3-laddning: {e}")
            return []
    
    def _play_id_audio(self):
        """Spela ID-ljudfil via CM108 - direkt metod"""
        try:
            if not os.path.exists(self.id_file):
                logger.error("ID-fil hittades inte")
                return
            
            # Konvertera MP3 till WAV med subprocess
            import tempfile
            import subprocess
            
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as temp_wav:
                temp_wav_path = temp_wav.name
            
            # Konvertera MP3 till WAV med ffmpeg
            cmd = [
                'ffmpeg', '-y', '-i', self.id_file,
                '-ar', str(self.sample_rate),
                '-ac', '1',
                '-f', 'wav',
                temp_wav_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                logger.error(f"Fel vid konvertering: {result.stderr}")
                # Fallback: försök spela original MP3 ändå
                return self._play_id_simple()
            
            # Aktivera PTT
            if self.cm108:
                self.cm108.set_ptt(True)
            
            time.sleep(0.1)  # PTT delay
            
            # Spela WAV-fil direkt via CM108
            import wave
            
            with wave.open(temp_wav_path, 'rb') as wav_file:
                frames = wav_file.getnframes()
                duration = frames / wav_file.getframerate()
                logger.info(f"ID-fil: {duration:.2f} sekunder")
                
                # Säkerhetskontroll - max 10 sekunder
                if duration > 10:
                    logger.warning("ID-fil för lång, begränsar till 10 sekunder")
                    max_frames = int(10 * wav_file.getframerate())
                else:
                    max_frames = frames
                
                # Spela via CM108
                if isinstance(self.output_device, int):
                    output_device = self.output_device
                else:
                    output_device = None
                
                stream = self.audio.open(
                    format=self.audio_format,
                    channels=1,
                    rate=self.sample_rate,
                    output=True,
                    output_device_index=output_device,
                    frames_per_buffer=self.chunk_size
                )
                
                # Spela chunks
                frames_played = 0
                
                while frames_played < max_frames:
                    chunk = wav_file.readframes(self.chunk_size)
                    if not chunk:
                        break
                    
                    # Volymjustering
                    adjusted_chunk = self.adjust_volume(chunk, self.output_volume)
                    stream.write(adjusted_chunk)
                    
                    frames_played += self.chunk_size
                
                stream.stop_stream()
                stream.close()
                
                actual_duration = frames_played / self.sample_rate
                logger.info(f"ID-uppspelning klar - {actual_duration:.2f} sekunder")
            
            # Rensa temp-fil
            os.unlink(temp_wav_path)
            
        except Exception as e:
            logger.error(f"Fel vid ID-uppspelning: {e}")
        finally:
            # Släpp PTT omedelbart
            if self.cm108:
                self.cm108.set_ptt(False)
            self.is_playing_id = False
            self.broadcast_status()
    
    def _play_id_simple(self):
        """Enkel ID-uppspelning som fallback"""
        try:
            # Aktivera PTT
            if self.cm108:
                self.cm108.set_ptt(True)
            
            time.sleep(0.1)  # PTT delay
            
            # Spela en enkel ton som ID istället
            import math
            import struct
            
            # Generera 2 sekunder 800Hz ton
            duration = 2.0
            frequency = 800
            
            samples = int(duration * self.sample_rate)
            
            if isinstance(self.output_device, int):
                output_device = self.output_device
            else:
                output_device = None
            
            stream = self.audio.open(
                format=self.audio_format,
                channels=1,
                rate=self.sample_rate,
                output=True,
                output_device_index=output_device,
                frames_per_buffer=self.chunk_size
            )
            
            # Generera ton
            for i in range(0, samples, self.chunk_size):
                chunk_samples = min(self.chunk_size, samples - i)
                chunk_data = []
                
                for j in range(chunk_samples):
                    sample_index = i + j
                    # Generera sinuston
                    value = int(16384 * math.sin(2 * math.pi * frequency * sample_index / self.sample_rate))
                    chunk_data.append(value)
                
                # Konvertera till bytes
                chunk_bytes = struct.pack('<' + 'h' * len(chunk_data), *chunk_data)
                
                # Volymjustering
                adjusted_chunk = self.adjust_volume(chunk_bytes, self.output_volume * 0.5)
                stream.write(adjusted_chunk)
            
            stream.stop_stream()
            stream.close()
            
            logger.info("ID-ton spelad (fallback)")
            
        except Exception as e:
            logger.error(f"Fel vid ton-generering: {e}")
        finally:
            # Släpp PTT omedelbart
            if self.cm108:
                self.cm108.set_ptt(False)
            self.is_playing_id = False
            self.broadcast_status()
        """Spela ID-ljudfil"""
        try:
            if self.cm108:
                self.cm108.set_ptt(True)
            
            time.sleep(0.2)
            
            # Spela MP3-fil med pygame
            pygame.mixer.music.load(self.id_file)
            pygame.mixer.music.play()
            
            # Vänta tills uppspelning är klar
            while pygame.mixer.music.get_busy():
                time.sleep(0.1)
            
            time.sleep(0.2)
            
        except Exception as e:
            logger.error(f"Fel vid ID-uppspelning: {e}")
        finally:
            if self.cm108:
                self.cm108.set_ptt(False)
            self.is_playing_id = False
            self.broadcast_status()
    
    def check_id_timer(self):
        """Kontrollera om det är dags för automatisk ID"""
        if not self.id_enabled:
            return
        
        if datetime.now() - self.last_id_time > timedelta(seconds=self.id_interval):
            self.play_station_id()
    
    def get_web_status(self):
        """Hämta status för webbgränssnitt"""
        uptime = datetime.now() - self.stats['uptime_start']
        
        return {
            'cos_active': self.cm108.last_cos_state if self.cm108 else False,
            'is_receiving': self.is_receiving,
            'is_transmitting': self.is_transmitting,
            'is_playing_id': self.is_playing_id,
            'input_volume': self.input_volume,
            'output_volume': self.output_volume,
            'id_enabled': self.id_enabled,
            'id_interval': self.id_interval,
            'id_file_exists': os.path.exists(self.id_file),
            'cm108_connected': self.cm108 is not None and self.cm108.device is not None,
            'stats': {
                'total_transmissions': self.stats['total_transmissions'],
                'total_receptions': self.stats['total_receptions'],
                'uptime': str(uptime).split('.')[0],
                'last_activity': self.stats['last_activity'].strftime('%H:%M:%S') if self.stats['last_activity'] else 'Ingen',
                'next_id': (self.last_id_time + timedelta(seconds=self.id_interval)).strftime('%H:%M:%S') if self.id_enabled else 'Inaktiv'
            }
        }
    
    def broadcast_status(self):
        """Skicka status till alla webbklienter"""
        try:
            if hasattr(self, 'socketio'):
                self.socketio.emit('status', self.get_web_status())
        except Exception as e:
            # Tysta fel om WebSocket inte fungerar
            pass
    
    def run(self):
        """Huvudloop för repeatern"""
        logger.info("SA818 Simplex Repeater med webbgränssnitt startad")
        logger.info("Webbgränssnitt: http://localhost:5000")
        
        try:
            while True:
                # Kontrollera ID-timer
                self.check_id_timer()
                
                # Broadcast status varje sekund
                self.broadcast_status()
                
                time.sleep(1)
                
        except KeyboardInterrupt:
            logger.info("Avslutar...")
            self.cleanup()
    
    def cleanup(self):
        """Städa upp resurser"""
        self.pre_recording = False
        
        if self.cm108:
            self.cm108.set_ptt(False)
            self.cm108.stop_monitoring()
        
        pygame.mixer.quit()
        self.audio.terminate()
        
        logger.info("Cleanup genomförd")

# HTML-mall för webbgränssnittet
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="sv">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SA818 Repeater Kontroll</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        
        .header {
            text-align: center;
            color: white;
            margin-bottom: 30px;
        }
        
        .header h1 {
            font-size: 2.5em;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }
        
        .header p {
            font-size: 1.2em;
            opacity: 0.9;
        }
        
        .dashboard {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .card {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 15px;
            padding: 25px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.2);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.3);
        }
        
        .card h3 {
            color: #4a5568;
            margin-bottom: 20px;
            font-size: 1.3em;
            border-bottom: 2px solid #e2e8f0;
            padding-bottom: 10px;
        }
        
        .status-indicator {
            display: flex;
            align-items: center;
            margin: 10px 0;
            padding: 10px;
            border-radius: 8px;
            font-weight: 500;
        }
        
        .status-indicator.active {
            background: #c6f6d5;
            color: #22543d;
        }
        
        .status-indicator.inactive {
            background: #fed7d7;
            color: #742a2a;
        }
        
        .status-indicator.neutral {
            background: #e2e8f0;
            color: #4a5568;
        }
        
        .status-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            margin-right: 10px;
            animation: pulse 2s infinite;
        }
        
        .status-dot.green { background: #48bb78; }
        .status-dot.red { background: #f56565; }
        .status-dot.blue { background: #4299e1; }
        .status-dot.gray { background: #a0aec0; }
        
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        
        .volume-control {
            margin: 15px 0;
        }
        
        .volume-control label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: #4a5568;
        }
        
        .volume-slider {
            width: 100%;
            height: 8px;
            border-radius: 5px;
            background: #e2e8f0;
            outline: none;
            -webkit-appearance: none;
        }
        
        .volume-slider::-webkit-slider-thumb {
            appearance: none;
            width: 20px;
            height: 20px;
            border-radius: 50%;
            background: #4299e1;
            cursor: pointer;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3);
        }
        
        .volume-value {
            float: right;
            font-weight: bold;
            color: #4299e1;
        }
        
        .btn {
            background: linear-gradient(135deg, #4299e1, #3182ce);
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 8px;
            font-size: 1em;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(66, 153, 225, 0.3);
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(66, 153, 225, 0.4);
        }
        
        .btn:active {
            transform: translateY(0);
        }
        
        .btn.danger {
            background: linear-gradient(135deg, #f56565, #e53e3e);
            box-shadow: 0 4px 15px rgba(245, 101, 101, 0.3);
        }
        
        .btn.success {
            background: linear-gradient(135deg, #48bb78, #38a169);
            box-shadow: 0 4px 15px rgba(72, 187, 120, 0.3);
        }
        
        .id-controls {
            display: flex;
            gap: 10px;
            margin-top: 15px;
        }
        
        .id-controls input {
            flex: 1;
            padding: 10px;
            border: 2px solid #e2e8f0;
            border-radius: 8px;
            font-size: 1em;
        }
        
        .id-controls input:focus {
            outline: none;
            border-color: #4299e1;
        }
        
        .checkbox-container {
            display: flex;
            align-items: center;
            margin: 15px 0;
        }
        
        .checkbox-container input[type="checkbox"] {
            margin-right: 10px;
            transform: scale(1.2);
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }
        
        .stat-item {
            text-align: center;
            padding: 15px;
            background: #f7fafc;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
        }
        
        .stat-value {
            font-size: 1.5em;
            font-weight: bold;
            color: #4299e1;
        }
        
        .stat-label {
            font-size: 0.9em;
            color: #718096;
            margin-top: 5px;
        }
        
        .footer {
            text-align: center;
            color: white;
            margin-top: 30px;
            opacity: 0.8;
        }
        
        @media (max-width: 768px) {
            .dashboard {
                grid-template-columns: 1fr;
            }
            
            .header h1 {
                font-size: 2em;
            }
            
            .id-controls {
                flex-direction: column;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎙️ SA818 Repeater</h1>
            <p>Moderna kontrollpanel för simplex repeater</p>
        </div>
        
        <div class="dashboard">
            <!-- Status kort -->
            <div class="card">
                <h3>📡 Repeaterstatus</h3>
                <div id="cos-status" class="status-indicator neutral">
                    <div class="status-dot gray"></div>
                    COS: Inaktiv
                </div>
                <div id="rx-status" class="status-indicator neutral">
                    <div class="status-dot gray"></div>
                    Mottagning: Inaktiv
                </div>
                <div id="tx-status" class="status-indicator neutral">
                    <div class="status-dot gray"></div>
                    Sändning: Inaktiv
                </div>
                <div id="id-status" class="status-indicator neutral">
                    <div class="status-dot gray"></div>
                    ID-uppspelning: Inaktiv
                </div>
                <div id="cm108-status" class="status-indicator neutral">
                    <div class="status-dot gray"></div>
                    CM108: Frånkopplad
                </div>
            </div>
            
            <!-- Volymkontroll -->
            <div class="card">
                <h3>🔊 Volymkontroll</h3>
                <div class="volume-control">
                    <label for="input-volume">
                        Inspelningsvolym 
                        <span class="volume-value" id="input-volume-value">1.0</span>
                    </label>
                    <input type="range" id="input-volume" class="volume-slider" 
                           min="0" max="2" step="0.1" value="1.0">
                </div>
                <div class="volume-control">
                    <label for="output-volume">
                        Utsändningsvolym 
                        <span class="volume-value" id="output-volume-value">1.2</span>
                    </label>
                    <input type="range" id="output-volume" class="volume-slider" 
                           min="0" max="2" step="0.1" value="1.2">
                </div>
            </div>
            
            <!-- Identifiering -->
            <div class="card">
                <h3>🎵 Stations-ID</h3>
                <div class="checkbox-container">
                    <input type="checkbox" id="id-enabled" checked>
                    <label for="id-enabled">Aktivera automatisk ID</label>
                </div>
                <div class="id-controls">
                    <input type="number" id="id-interval" placeholder="Intervall (sekunder)" 
                           value="600" min="60" max="3600">
                    <button class="btn" onclick="updateIdSettings()">Uppdatera</button>
                </div>
                <div class="id-controls">
                    <button class="btn success" onclick="triggerManualId()">Spela ID Nu</button>
                    <span id="id-file-status" style="color: #718096;">Kontrollerar fil...</span>
                </div>
            </div>
            
            <!-- Statistik -->
            <div class="card">
                <h3>📊 Statistik</h3>
                <div class="stats-grid">
                    <div class="stat-item">
                        <div class="stat-value" id="total-rx">0</div>
                        <div class="stat-label">Mottagningar</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="total-tx">0</div>
                        <div class="stat-label">Sändningar</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="uptime">00:00:00</div>
                        <div class="stat-label">Drifttid</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="last-activity">Ingen</div>
                        <div class="stat-label">Senaste aktivitet</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="next-id">Inaktiv</div>
                        <div class="stat-label">Nästa ID</div>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="footer">
            <p>SA818 Simplex Repeater v2.0 - Webbgränssnitt</p>
            <p>Anslutningsstatus: <span id="connection-status">Ansluter...</span></p>
        </div>
    </div>
    
    <script>
        // Socket.IO anslutning
        const socket = io();
        
        // Anslutningsstatus
        socket.on('connect', function() {
            document.getElementById('connection-status').textContent = 'Ansluten';
            document.getElementById('connection-status').style.color = '#48bb78';
        });
        
        socket.on('disconnect', function() {
            document.getElementById('connection-status').textContent = 'Frånkopplad';
            document.getElementById('connection-status').style.color = '#f56565';
        });
        
        // Statusuppdateringar
        socket.on('status', function(data) {
            updateStatus(data);
        });
        
        function updateStatus(data) {
            // COS status
            updateStatusIndicator('cos-status', data.cos_active, 'COS');
            
            // RX status
            updateStatusIndicator('rx-status', data.is_receiving, 'Mottagning');
            
            // TX status
            updateStatusIndicator('tx-status', data.is_transmitting, 'Sändning');
            
            // ID status
            updateStatusIndicator('id-status', data.is_playing_id, 'ID-uppspelning');
            
            // CM108 status
            updateStatusIndicator('cm108-status', data.cm108_connected, 'CM108');
            
            // Volymvärden
            document.getElementById('input-volume').value = data.input_volume;
            document.getElementById('input-volume-value').textContent = data.input_volume.toFixed(1);
            document.getElementById('output-volume').value = data.output_volume;
            document.getElementById('output-volume-value').textContent = data.output_volume.toFixed(1);
            
            // ID-inställningar
            document.getElementById('id-enabled').checked = data.id_enabled;
            document.getElementById('id-interval').value = data.id_interval;
            document.getElementById('id-file-status').textContent = 
                data.id_file_exists ? '✅ station_id.mp3 hittad' : '❌ station_id.mp3 saknas';
            
            // Statistik
            document.getElementById('total-rx').textContent = data.stats.total_receptions;
            document.getElementById('total-tx').textContent = data.stats.total_transmissions;
            document.getElementById('uptime').textContent = data.stats.uptime;
            document.getElementById('last-activity').textContent = data.stats.last_activity;
            document.getElementById('next-id').textContent = data.stats.next_id;
        }
        
        function updateStatusIndicator(elementId, isActive, label) {
            const element = document.getElementById(elementId);
            const dot = element.querySelector('.status-dot');
            
            if (isActive) {
                element.className = 'status-indicator active';
                dot.className = 'status-dot green';
                element.innerHTML = `<div class="status-dot green"></div>${label}: Aktiv`;
            } else {
                element.className = 'status-indicator inactive';
                dot.className = 'status-dot gray';
                element.innerHTML = `<div class="status-dot gray"></div>${label}: Inaktiv`;
            }
        }
        
        // Volymkontroller
        document.getElementById('input-volume').addEventListener('input', function() {
            const value = parseFloat(this.value);
            document.getElementById('input-volume-value').textContent = value.toFixed(1);
            
            fetch('/api/volume', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    input: value
                })
            });
        });
        
        document.getElementById('output-volume').addEventListener('input', function() {
            const value = parseFloat(this.value);
            document.getElementById('output-volume-value').textContent = value.toFixed(1);
            
            fetch('/api/volume', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    output: value
                })
            });
        });
        
        // ID-funktioner
        function updateIdSettings() {
            const enabled = document.getElementById('id-enabled').checked;
            const interval = parseInt(document.getElementById('id-interval').value);
            
            fetch('/api/id', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    enabled: enabled,
                    interval: interval
                })
            });
        }
        
        function triggerManualId() {
            fetch('/api/id', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    trigger: true
                })
            });
        }
        
        // Auto-uppdatera ID-inställningar när checkbox ändras
        document.getElementById('id-enabled').addEventListener('change', updateIdSettings);
        
        // Hämta initial status
        fetch('/api/status')
            .then(response => response.json())
            .then(data => updateStatus(data));
        
        // Uppdatera status regelbundet som backup
        setInterval(function() {
            fetch('/api/status')
                .then(response => response.json())
                .then(data => updateStatus(data));
        }, 5000);
    </script>
</body>
</html>
'''

def main():
    """Huvudfunktion"""
    try:
        repeater = SA818Repeater()
        repeater.run()
    except Exception as e:
        logger.error(f"Fel vid start: {e}")

if __name__ == "__main__":
    main()
