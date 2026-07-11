#!/usr/bin/env python3
"""
Fan Control GUI for ec_su_axb35 driver
Requires appropriate permissions to read/write /sys/class/ec_su_axb35/
"""
import os
import sys
import subprocess

def ensure_root():
    if os.geteuid() == 0:
        return  # already root

    # Prevent infinite relaunch loop
    if os.environ.get("EC_FAN_CONTROL_ROOT") == "1":
        sys.exit("Failed to gain root privileges")

    env = os.environ.copy()
    env["EC_FAN_CONTROL_ROOT"] = "1"

    cmd = [
        "pkexec",
        "env",
        f"DISPLAY={env.get('DISPLAY', '')}",
        f"XAUTHORITY={env.get('XAUTHORITY', '')}",
        sys.executable,
        os.path.abspath(__file__)
    ]

    subprocess.run(cmd, env=env)
    sys.exit(0)

ensure_root()

import argparse
import json
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import queue
import os

CONFIG_PATH = "/etc/ec-fan-control.json"
BASE_PATH = "/sys/class/ec_su_axb35"

class FanControlGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Fan Control - ec_su_axb35")
        self.root.geometry("900x800")
        
        self.config_path = CONFIG_PATH
        self.base_path = BASE_PATH
        self.update_interval = 1.0
        self.running = True
        self.stop_event = threading.Event()
        self.ui_queue = queue.Queue()
        self.ui_after_id = None
        self.mode_check_timer = None
        self.fan_mode_check_timers = {}
        self.curve_write_delay = 0.4  # seconds
        # (fan, curve) -> generation counter
        self.curve_write_gen = {}
        # (fan, curve) -> threading.Timer
        self.curve_write_timers = {}
        
        # Create GUI
        self.create_widgets()

        # Only the main thread may interact with Tk. Worker threads put UI
        # callbacks on this queue, which is drained from the Tk event loop.
        self.ui_after_id = self.root.after(50, self.process_ui_queue)
        
        # Start monitoring thread
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()
        
        # Initial mode read
        self.read_all_modes()

    def queue_ui(self, callback):
        """Queue a Tk callback for execution by the main thread."""
        if self.running:
            self.ui_queue.put(callback)

    def process_ui_queue(self):
        """Execute callbacks queued by worker threads."""
        if not self.running:
            return

        while True:
            try:
                callback = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            callback()

        self.ui_after_id = self.root.after(50, self.process_ui_queue)
        
    def read_sysfs(self, path):
        """Read value from sysfs file"""
        try:
            with open(path, 'r') as f:
                return f.read().strip()
        except Exception as e:
            print(f"Error reading {path}: {e}")
            return None
    
    def write_sysfs(self, path, value):
        """Write value to sysfs file"""
        try:
            with open(path, 'w') as f:
                f.write(str(value))
            return True
        except Exception as e:
            self.queue_ui(lambda error=e: messagebox.showerror(
                "Error", f"Failed to write to {path}: {error}"
            ))
            return False
    
    def create_widgets(self):
        # Top frame - Temperature and Fan RPMs
        top_frame = ttk.LabelFrame(self.root, text="System Status", padding=10)
        top_frame.pack(fill=tk.X, padx=10, pady=5)
        
        # Temperature
        ttk.Label(top_frame, text="CPU Temperature:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.temp_label = ttk.Label(top_frame, text="--°C", font=('Arial', 12, 'bold'))
        self.temp_label.grid(row=0, column=1, sticky=tk.W, padx=5)
        
        # Fan RPMs
        ttk.Label(top_frame, text="Fan 1 RPM:").grid(row=0, column=2, sticky=tk.W, padx=5)
        self.fan1_rpm_label = ttk.Label(top_frame, text="----", font=('Arial', 12, 'bold'))
        self.fan1_rpm_label.grid(row=0, column=3, sticky=tk.W, padx=5)
        
        ttk.Label(top_frame, text="Fan 2 RPM:").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.fan2_rpm_label = ttk.Label(top_frame, text="----", font=('Arial', 12, 'bold'))
        self.fan2_rpm_label.grid(row=1, column=1, sticky=tk.W, padx=5)
        
        ttk.Label(top_frame, text="Fan 3 RPM:").grid(row=1, column=2, sticky=tk.W, padx=5)
        self.fan3_rpm_label = ttk.Label(top_frame, text="----", font=('Arial', 12, 'bold'))
        self.fan3_rpm_label.grid(row=1, column=3, sticky=tk.W, padx=5)
        
        # Update interval selector
        ttk.Label(top_frame, text="Update Interval:").grid(row=0, column=4, sticky=tk.W, padx=5)
        self.interval_var = tk.StringVar(value="1")
        interval_combo = ttk.Combobox(top_frame, textvariable=self.interval_var, 
                                      values=["0.5", "1", "2", "5"], width=5, state='readonly')
        interval_combo.grid(row=0, column=5, sticky=tk.W, padx=5)
        interval_combo.bind('<<ComboboxSelected>>', self.on_interval_change)
        ttk.Label(top_frame, text="sec").grid(row=0, column=6, sticky=tk.W)
        
        # APU Power Mode frame
        apu_frame = ttk.LabelFrame(self.root, text="APU Power Mode", padding=10)
        apu_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(apu_frame, text="Power Mode:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.apu_mode_var = tk.StringVar()
        self.apu_mode_combo = ttk.Combobox(apu_frame, textvariable=self.apu_mode_var,
                                           values=["quiet", "balanced", "performance"], 
                                           width=15, state='readonly')
        self.apu_mode_combo.grid(row=0, column=1, sticky=tk.W, padx=5)
        self.apu_mode_combo.bind('<<ComboboxSelected>>', self.on_apu_mode_change)
        
        # Fan control blocks
        fans_frame = ttk.Frame(self.root)
        fans_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.fan_controls = {}
        for i, fan_num in enumerate([1, 2, 3], 1):
            fan_name = ["CPU Fan 1", "CPU Fan 2", "System Fan"][i-1]
            self.create_fan_control(fans_frame, fan_num, fan_name, i-1)

        # Config control block
        config_frame = ttk.Frame(self.root)
        config_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Button(
            config_frame,
            text="Save Config",
            command=self.save_config
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            config_frame,
            text="Load Config",
            command=self.load_config
        ).pack(side=tk.LEFT, padx=5)
    
    def create_fan_control(self, parent, fan_num, fan_name, column):
        """Create control block for a single fan"""
        frame = ttk.LabelFrame(parent, text=fan_name, padding=10)
        frame.grid(row=0, column=column, sticky=tk.NSEW, padx=5)
        parent.columnconfigure(column, weight=1)
        parent.rowconfigure(0, weight=1)
        
        # RPM display
        ttk.Label(frame, text="Current RPM:").pack()
        rpm_label = ttk.Label(frame, text="----", font=('Arial', 14, 'bold'))
        rpm_label.pack()
        
        # Mode selector
        ttk.Label(frame, text="Fan Mode:", font=('Arial', 10, 'bold')).pack(pady=(10, 0))
        mode_var = tk.StringVar()
        mode_combo = ttk.Combobox(frame, textvariable=mode_var,
                                  values=["auto", "fixed", "curve"], 
                                  width=12, state='readonly')
        mode_combo.pack(pady=5)
        mode_combo.bind('<<ComboboxSelected>>', 
                       lambda e, fn=fan_num: self.on_fan_mode_change(fn))
        
        # Level selector (for fixed mode)
        level_frame = ttk.Frame(frame)
        ttk.Label(level_frame, text="Level:").pack(side=tk.LEFT)
        level_var = tk.StringVar()
        level_combo = ttk.Combobox(level_frame, textvariable=level_var,
                                   values=["1", "2", "3", "4", "5"], 
                                   width=5, state='readonly')
        level_combo.pack(side=tk.LEFT, padx=5)
        level_combo.bind('<<ComboboxSelected>>', 
                        lambda e, fn=fan_num: self.on_level_change(fn))
        
        # Curve controls
        curve_frame = ttk.LabelFrame(frame, text="Fan Curves", padding=5)
        
        # Ramp up curve
        rampup_frame = ttk.Frame(curve_frame)
        rampup_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        ttk.Label(rampup_frame, text="Ramp Up (°C)").pack()
        
        rampup_sliders = []
        rampup_labels = []
        slider_frame = ttk.Frame(rampup_frame)
        slider_frame.pack(fill=tk.BOTH, expand=True)
        
        for i in range(5):
            col_frame = ttk.Frame(slider_frame)
            col_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2)
            
            value_label = ttk.Label(col_frame, text="--", font=('Arial', 9))
            value_label.pack()
            
            slider = tk.Scale(col_frame, from_=100, to=30, orient=tk.VERTICAL,
                            length=150, showvalue=0,
                            command=lambda v, fn=fan_num, idx=i, curve='rampup': 
                            self.on_curve_change(fn, curve, idx, v))
            slider.pack()
            
            label = ttk.Label(col_frame, text=f"L{i+1}")
            label.pack()
            
            rampup_sliders.append(slider)
            rampup_labels.append(value_label)
        
        # Ramp down curve
        rampdown_frame = ttk.Frame(curve_frame)
        rampdown_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        ttk.Label(rampdown_frame, text="Ramp Down (°C)").pack()
        
        rampdown_sliders = []
        rampdown_labels = []
        slider_frame = ttk.Frame(rampdown_frame)
        slider_frame.pack(fill=tk.BOTH, expand=True)
        
        for i in range(5):
            col_frame = ttk.Frame(slider_frame)
            col_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2)
            
            value_label = ttk.Label(col_frame, text="--", font=('Arial', 9))
            value_label.pack()
            
            slider = tk.Scale(col_frame, from_=100, to=30, orient=tk.VERTICAL,
                            length=150, showvalue=0,
                            command=lambda v, fn=fan_num, idx=i, curve='rampdown': 
                            self.on_curve_change(fn, curve, idx, v))
            slider.pack()
            
            label = ttk.Label(col_frame, text=f"L{i+1}")
            label.pack()
            
            rampdown_sliders.append(slider)
            rampdown_labels.append(value_label)
        
        # Store references
        self.fan_controls[fan_num] = {
            'rpm_label': rpm_label,
            'mode_var': mode_var,
            'mode_combo': mode_combo,
            'level_var': level_var,
            'level_combo': level_combo,
            'level_frame': level_frame,
            'curve_frame': curve_frame,
            'rampup_sliders': rampup_sliders,
            'rampup_labels': rampup_labels,
            'rampup_values': [None] * 5,
            'rampdown_sliders': rampdown_sliders,
            'rampdown_labels': rampdown_labels,
            'rampdown_values': [None] * 5,
        }
        _ = self.read_curve(fan_num, "rampup")
        _ = self.read_curve(fan_num, "rampdown")
        
        # Initially hide level and curve controls
        level_frame.pack_forget()
        curve_frame.pack_forget()
    
    def on_interval_change(self, event):
        """Handle update interval change"""
        self.update_interval = float(self.interval_var.get())
    
    def on_apu_mode_change(self, event):
        """Handle APU power mode change"""
        mode = self.apu_mode_var.get()
        path = f"{self.base_path}/apu/power_mode"
        if self.write_sysfs(path, mode):
            # Schedule mode verification after 10 seconds
            if self.mode_check_timer:
                self.mode_check_timer.cancel()
            self.mode_check_timer = threading.Timer(10.0, self.read_apu_mode)
            self.mode_check_timer.daemon = True
            self.mode_check_timer.start()
    
    def on_fan_mode_change(self, fan_num):
        """Handle fan mode change"""
        mode = self.fan_controls[fan_num]['mode_var'].get()
        path = f"{self.base_path}/fan{fan_num}/mode"
        
        if self.write_sysfs(path, mode):
            # Update UI based on mode
            self.update_fan_mode_ui(fan_num, mode)
            
            # If switching to curve mode, read current curve values
            if mode == "curve":
                self.read_fan_curves(fan_num)
            
            # Schedule mode verification after 10 seconds
            old_timer = self.fan_mode_check_timers.get(fan_num)
            if old_timer:
                old_timer.cancel()
            timer = threading.Timer(10.0, self.read_fan_mode, args=(fan_num,))
            timer.daemon = True
            self.fan_mode_check_timers[fan_num] = timer
            timer.start()
    
    def update_fan_mode_ui(self, fan_num, mode):
        """Show/hide controls based on fan mode"""
        controls = self.fan_controls[fan_num]
        
        # Hide everything first
        controls['level_frame'].pack_forget()
        controls['curve_frame'].pack_forget()
        
        # Show relevant controls based on mode
        if mode == "fixed":
            controls['level_frame'].pack(pady=5)
        elif mode == "curve":
            controls['curve_frame'].pack(fill=tk.BOTH, expand=True, pady=5)
    
    def on_level_change(self, fan_num):
        """Handle fan level change"""
        level = self.fan_controls[fan_num]['level_var'].get()
        path = f"{self.base_path}/fan{fan_num}/level"
        self.write_sysfs(path, level)

    def schedule_curve_write(self, fan_num, curve_type, values):
        key = (fan_num, curve_type)

        # Increment generation
        gen = self.curve_write_gen.get(key, 0) + 1
        self.curve_write_gen[key] = gen

        # Cancel old timer if it exists
        old_timer = self.curve_write_timers.get(key)
        if old_timer:
            old_timer.cancel()

        def do_write(expected_gen):
            # Discard if superseded
            if self.curve_write_gen.get(key) != expected_gen:
                return

            curve_str = ",".join(map(str, values))
            path = f"{self.base_path}/fan{fan_num}/{curve_type}_curve"

            self.write_sysfs(path, curve_str)

        timer = threading.Timer(
            self.curve_write_delay,
            lambda: do_write(gen)
        )
        timer.daemon = True

        self.curve_write_timers[key] = timer
        timer.start()

    def on_curve_change(self, fan_num, curve_type, index, value):
        """Handle curve slider change with cascading constraints"""
        controls = self.fan_controls[fan_num]
        sliders = controls[f'{curve_type}_sliders']
        labels = controls[f'{curve_type}_labels']
        values = controls[f'{curve_type}_values']
        other_curve = 'rampdown' if curve_type == 'rampup' else 'rampup'
        other_sliders = controls[f'{other_curve}_sliders']
        other_labels = controls[f'{other_curve}_labels']
        other_values = controls[f'{other_curve}_values']
        new_value = int(float(value))
        other_modified = False
        
        # Apply cascading constraints
        # If we're moving a slider, ensure all subsequent sliders are >= this value
        if new_value != values[index]:
            values[index] = new_value
            
            # Enforce minimum constraint: each level must be >= previous level
            for i in range(index + 1, 5):
                if values[i] < values[index]:
                    values[i] = values[index]
                    # Temporarily block the callback to avoid recursion
                    sliders[i].config(command='')
                    sliders[i].set(values[i])
                    sliders[i].config(command=lambda v, fn=fan_num, idx=i, curve=curve_type: 
                                     self.on_curve_change(fn, curve, idx, v))
            
            # Also check backwards: if previous levels are > current, adjust them
            for i in range(index - 1, -1, -1):
                if values[i] > values[index]:
                    values[i] = values[index]
                    # Temporarily block the callback to avoid recursion
                    sliders[i].config(command='')
                    sliders[i].set(values[i])
                    sliders[i].config(command=lambda v, fn=fan_num, idx=i, curve=curve_type: 
                                     self.on_curve_change(fn, curve, idx, v))

            # Enforce rampup/rampdown constraint: rampdown <= rampup for each level            
            if curve_type == 'rampup':
                for i in range(5):
                    if other_values[i] > values[i]:
                        other_values[i] = values[i]
                        other_sliders[i].config(command='')
                        other_sliders[i].set(other_values[i])
                        other_sliders[i].config(command=lambda v, fn=fan_num, idx=i, curve=other_curve: 
                                               self.on_curve_change(fn, curve, idx, v))
                        other_modified = True
            else:  # curve_type == 'rampdown'
                for i in range(5):
                    if other_values[i] < values[i]:
                        other_values[i] = values[i]
                        other_sliders[i].config(command='')
                        other_sliders[i].set(other_values[i])
                        other_sliders[i].config(command=lambda v, fn=fan_num, idx=i, curve=other_curve: 
                                               self.on_curve_change(fn, curve, idx, v))
                        other_modified = True

            # If other was modified, enforce cascading constraint on it
            if other_modified:
                for i in range(4):
                    if other_values[i+1] < other_values[i]:
                        other_values[i+1] = other_values[i]
                        other_sliders[i+1].config(command='')
                        other_sliders[i+1].set(other_values[i+1])
                        other_sliders[i+1].config(command=lambda v, fn=fan_num, idx=i+1, curve=other_curve: 
                                                self.on_curve_change(fn, curve, idx, v))
        
        # Update all labels
        for i, val in enumerate(values):
            labels[i].config(text=f"{val}°C")
        for i, val in enumerate(other_values):
            other_labels[i].config(text=f"{val}°C")
        
        # Schedule Write all values to sysfs
        self.schedule_curve_write(fan_num, curve_type, values)
        controls[f'{curve_type}_values'] = values
        
        # Also write the other curve if we modified it
        if other_modified:
            self.schedule_curve_write(fan_num, other_curve, other_values)
            controls[f'{other_curve}_values'] = other_values
    
    def read_curve(self, fan_num, curve_type):
        """Read a fan curve (rampup_curve or rampdown_curve)"""
        path = f"{self.base_path}/fan{fan_num}/{curve_type}_curve"
        data = self.read_sysfs(path)
        if data:
            try:
                values = [int(x) for x in data.replace(',', ' ').split()]
                self.fan_controls[fan_num][f'{curve_type}_values'] = values
                return values
            except:
                return None
        return None
    
    def read_fan_curves(self, fan_num):
        """Read and update curve sliders for a fan"""
        rampup = self.read_curve(fan_num, "rampup")
        rampdown = self.read_curve(fan_num, "rampdown")
        
        controls = self.fan_controls[fan_num]
        
        if rampup and len(rampup) == 5:
            for i, value in enumerate(rampup):
                controls['rampup_sliders'][i].set(value)
                controls['rampup_labels'][i].config(text=f"{value}°C")
        
        if rampdown and len(rampdown) == 5:
            for i, value in enumerate(rampdown):
                controls['rampdown_sliders'][i].set(value)
                controls['rampdown_labels'][i].config(text=f"{value}°C")
    
    def read_fan_mode(self, fan_num):
        """Read current fan mode"""
        path = f"{self.base_path}/fan{fan_num}/mode"
        mode = self.read_sysfs(path)
        if mode:
            # Extract current mode from [auto, fixed, curve] format
            if '[' in mode:
                for m in ['auto', 'fixed', 'curve']:
                    if f'[{m}]' in mode:
                        mode = m
                        break
            self.queue_ui(lambda m=mode: self.fan_controls[fan_num]['mode_var'].set(m))
            self.queue_ui(lambda m=mode: self.update_fan_mode_ui(fan_num, m))
            # If mode is curve, read the current curve values
            if mode == 'curve':
                self.queue_ui(lambda: self.read_fan_curves(fan_num))
    
    def read_all_modes(self):
        """Read all fan modes and APU mode"""
        for fan_num in [1, 2, 3]:
            self.read_fan_mode(fan_num)
        self.read_apu_mode()
    
    def read_apu_mode(self):
        """Read current APU power mode"""
        path = f"{self.base_path}/apu/power_mode"
        mode = self.read_sysfs(path)
        if mode:
            # Extract current mode from [quiet, balanced, performance] format
            if '[' in mode:
                for m in ['quiet', 'balanced', 'performance']:
                    if f'[{m}]' in mode:
                        mode = m
                        break
            self.queue_ui(lambda m=mode: self.apu_mode_var.set(m))
    
    def monitor_loop(self):
        """Background thread to monitor temperature and RPM"""
        while self.running:
            try:
                # Read temperature
                temp_path = f"{self.base_path}/temp1/temp"
                temp = self.read_sysfs(temp_path)
                if temp:
                    self.queue_ui(lambda t=temp: self.temp_label.config(text=f"{t}°C"))
                
                # Read fan RPMs
                for fan_num in [1, 2, 3]:
                    rpm_path = f"{self.base_path}/fan{fan_num}/rpm"
                    rpm = self.read_sysfs(rpm_path)
                    if rpm:
                        if fan_num == 1:
                            self.queue_ui(lambda r=rpm: self.fan1_rpm_label.config(text=r))
                        elif fan_num == 2:
                            self.queue_ui(lambda r=rpm: self.fan2_rpm_label.config(text=r))
                        elif fan_num == 3:
                            self.queue_ui(lambda r=rpm: self.fan3_rpm_label.config(text=r))
                        
                        # Update fan control block RPM
                        self.queue_ui(lambda fn=fan_num, r=rpm:
                                      self.fan_controls[fn]['rpm_label'].config(text=r))
                
            except Exception as e:
                print(f"Monitor error: {e}")
            
            self.stop_event.wait(self.update_interval)

    def save_config(self):
        data = {
            "apu_mode": self.apu_mode_var.get(),
            "fans": {}
        }

        for fan_num, c in self.fan_controls.items():
            data["fans"][str(fan_num)] = {
                "mode": c["mode_var"].get(),
                "level": c["level_var"].get(),
                "rampup_curve": c["rampup_values"][:],
                "rampdown_curve": c["rampdown_values"][:],
            }

        try:
            with open(self.config_path, "w") as f:
                json.dump(data, f, indent=2)
            messagebox.showinfo("Saved", "Configuration saved successfully.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save config:\n{e}")

    def load_config(self):
        try:
            with open(self.config_path) as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load config:\n{e}")
            return

        # APU mode
        apu = data.get("apu_mode")
        if apu:
            self.apu_mode_var.set(apu)
            self.write_sysfs(f"{self.base_path}/apu/power_mode", apu)

        for fan_num_str, cfg in data.get("fans", {}).items():
            fan_num = int(fan_num_str)
            controls = self.fan_controls.get(fan_num)
            if not controls:
                continue

            mode = cfg.get("mode")
            if mode:
                controls["mode_var"].set(mode)
                self.write_sysfs(f"{self.base_path}/fan{fan_num}/mode", mode)
                self.update_fan_mode_ui(fan_num, mode)

            level = cfg.get("level")
            if level and mode == "fixed":
                controls["level_var"].set(level)
                self.write_sysfs(f"{self.base_path}/fan{fan_num}/level", level)

            # Curves
            if mode == "curve":
                ru = cfg.get("rampup_curve")
                rd = cfg.get("rampdown_curve")

                if ru and len(ru) == 5:
                    controls["rampup_values"][:] = ru
                    for i, s in enumerate(controls["rampup_sliders"]):
                        s.set(ru[i])
                    self.schedule_curve_write(fan_num, "rampup", ru)

                if rd and len(rd) == 5:
                    controls["rampdown_values"][:] = rd
                    for i, s in enumerate(controls["rampdown_sliders"]):
                        s.set(rd[i])
                    self.schedule_curve_write(fan_num, "rampdown", rd)
        messagebox.showinfo("Loaded", "Configuration loaded successfully.")
    
    def on_closing(self):
        """Handle window close"""
        self.running = False
        self.stop_event.set()
        if self.mode_check_timer:
            self.mode_check_timer.cancel()
        for timer in self.fan_mode_check_timers.values():
            timer.cancel()
        for timer in self.curve_write_timers.values():
            timer.cancel()
        if self.ui_after_id is not None:
            self.root.after_cancel(self.ui_after_id)
            self.ui_after_id = None
        self.monitor_thread.join(timeout=1.0)
        self.root.destroy()

def main():
    root = tk.Tk()
    app = FanControlGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()

if __name__ == "__main__":
    main()
