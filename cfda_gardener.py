#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CFDA_Gardener - Prototipo de Chip con Refrigeración Fractal y Kernel de Coherencia
Ejecuta: python cfda_gardener.py
"""

import random
import time
import math
import sys
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from collections import deque

# =============================================================================
# 1. MODELO TÉRMICO (El Sumidero Real)
# =============================================================================
class ThermalModel:
    """Simula la temperatura de un core basada en energía disipada."""
    def __init__(self, ambient_temp=25.0, thermal_resistance=2.0, heat_capacity=5.0):
        self.temp = ambient_temp
        self.ambient = ambient_temp
        self.R = thermal_resistance    # °C/W
        self.C = heat_capacity         # J/°C
        self.power = 0.0

    def update(self, energy_nj: float, dt: float):
        """energy_nj: nanojulios consumidos en este ciclo. dt: segundos (típico 1e-9)."""
        power = energy_nj * 1e-9 / dt   # vatios
        self.power = power
        # Ecuación de enfriamiento newtoniano: dT/dt = (P - (T-Tamb)/R) / C
        heat_in = power * dt
        heat_out = (self.temp - self.ambient) / self.R * dt
        self.temp += (heat_in - heat_out) / self.C
        return self.temp

# =============================================================================
# 2. PREDICTOR CFDA (Stride + Markov)
# =============================================================================
class CFDAPredictor:
    def __init__(self):
        self.history = []
        self.stride_conf = 0
        self.markov_table = {}
        self.correct = 0
        self.total = 0

    def predict(self, addr: int) -> Optional[int]:
        self.total += 1
        self.history.append(addr)
        if len(self.history) > 32:
            self.history.pop(0)
        if len(self.history) < 3:
            return None

        # Stride
        s1 = self.history[-1] - self.history[-2]
        s2 = self.history[-2] - self.history[-3]
        if s1 == s2 and s1 != 0:
            self.correct += 1
            return addr + s1

        # Markov (simplificado)
        key = (self.history[-3], self.history[-2])
        if key in self.markov_table and self.markov_table[key]:
            best = max(self.markov_table[key], key=self.markov_table[key].get)
            self.correct += 1
            return best
        return None

    def update_markov(self):
        if len(self.history) >= 4:
            key = (self.history[-4], self.history[-3])
            nxt = self.history[-2]
            if key not in self.markov_table:
                self.markov_table[key] = {}
            self.markov_table[key][nxt] = self.markov_table[key].get(nxt, 0) + 1

# =============================================================================
# 3. CORE CFDA CON MEMORIA LOCAL Y PREDICTOR
# =============================================================================
class CFDACore:
    def __init__(self, core_id: int, position: Tuple[float, float]):
        self.id = core_id
        self.pos = position                # (x,y) para visualización φ
        self.predictor = CFDAPredictor()
        self.local_buffer = {}             # cache local
        self.buffer_size = 64
        self.accesses = 0
        self.hits = 0
        self.thermal = ThermalModel()
        self.current_process = None
        self.ipc = 0.0
        self.util = 0.0

    def access(self, addr: int) -> float:
        """Retorna energía consumida en nJ."""
        self.accesses += 1
        self.predictor.update_markov()

        # Prefetch si hay predicción
        pred = self.predictor.predict(addr)
        if pred is not None:
            self._prefetch(pred)

        line = addr // 64
        if line in self.local_buffer:
            self.hits += 1
            energy = 0.5   # nJ por hit en buffer local
            lat = 1        # ciclos
        else:
            # Miss: ir a "DRAM"
            self._load_line(line)
            energy = 50.0  # nJ por acceso a DRAM (mover datos quema energía)
            lat = 25

        # Actualizar temperatura
        self.thermal.update(energy, dt=0.5e-9)  # 0.5 ns por ciclo (2 GHz)
        self.ipc = 1.0 / lat if lat > 0 else 1.0
        return energy

    def _prefetch(self, addr: int):
        line = addr // 64
        if len(self.local_buffer) < self.buffer_size:
            self.local_buffer[line] = True

    def _load_line(self, line: int):
        if len(self.local_buffer) >= self.buffer_size:
            self.local_buffer.pop(next(iter(self.local_buffer)))
        self.local_buffer[line] = True

    def hit_rate(self):
        return self.hits / self.accesses if self.accesses > 0 else 0.0

# =============================================================================
# 4. PROCESO (Tarea que ejecuta accesos a memoria)
# =============================================================================
class Process:
    def __init__(self, pid: int, name: str, pattern: str, intensity: float):
        self.pid = pid
        self.name = name
        self.pattern = pattern      # "stream", "stride", "random"
        self.intensity = intensity  # probabilidad de acceso por ciclo
        self.addr = random.randint(0x1000, 0x10000)
        self.stride = random.choice([64, 128, 256, -128])
        self.progress = 0
        self.assigned_core = None

    def next_address(self) -> Optional[int]:
        if random.random() > self.intensity:
            return None
        if self.pattern == "stream":
            self.addr += 64
        elif self.pattern == "stride":
            self.addr += self.stride
        else:  # random
            self.addr = random.randint(0x1000, 0x100000)
        self.progress += 1
        return self.addr

# =============================================================================
# 5. KERNEL RELACIONAL (Scheduler de Coherencia Térmica Y(t))
# =============================================================================
class RelationalKernel:
    def __init__(self, cores: List[CFDACore], processes: List[Process]):
        self.cores = cores
        self.processes = processes
        self.history = deque(maxlen=50)

    def schedule(self):
        """Asigna procesos a cores para maximizar Y(t) = Σ (1/Temp_i) * IPC_i"""
        # Ordenar procesos por "hambre" (progreso pendiente)
        hungry = sorted(self.processes, key=lambda p: -p.intensity)
        # Ordenar cores por "frescura" (menor temperatura)
        cool_cores = sorted(self.cores, key=lambda c: c.thermal.temp)

        for p in hungry:
            if p.assigned_core is not None:
                continue  # ya está ejecutándose
            # Buscar core libre o el más frío
            for core in cool_cores:
                if core.current_process is None:
                    core.current_process = p
                    p.assigned_core = core
                    break

    def step(self):
        # 1. Ejecutar un ciclo en cada core ocupado
        for core in self.cores:
            if core.current_process is not None:
                proc = core.current_process
                addr = proc.next_address()
                if addr is not None:
                    energy = core.access(addr)
                else:
                    energy = 0.1  # idle
                    core.thermal.update(0.1, 0.5e-9)
                # Terminar proceso si ha hecho suficientes accesos
                if proc.progress > 200:
                    core.current_process = None
                    proc.assigned_core = None
                    proc.progress = 0
            else:
                core.thermal.update(0.05, 0.5e-9)  # idle bajo consumo

        # 2. Reasignar procesos si hay cores libres
        self.schedule()

        # 3. Calcular Y(t) global
        Y = self.compute_coherence()
        self.history.append(Y)
        return Y

    def compute_coherence(self) -> float:
        """Y(t) ∝ Σ (IPC_i / Temperatura_i)  - queremos alto rendimiento con baja temperatura"""
        total = 0.0
        for core in self.cores:
            if core.thermal.temp > 0:
                total += core.ipc / (core.thermal.temp / 30.0)  # normalizado a 30°C
        return total / len(self.cores)

# =============================================================================
# 6. VISUALIZACIÓN ASCII (El Jardín Fractal)
# =============================================================================
def render_garden(kernel: RelationalKernel, cycle: int):
    # Limpiar pantalla
    sys.stdout.write("\033[2J\033[H")
    print(f"🌿 CFDA_Gardener - Ciclo {cycle} | Coherencia Y(t) = {kernel.history[-1]:.3f}")
    print("=" * 70)

    # Ordenar cores por posición (espiral φ)
    sorted_cores = sorted(kernel.cores, key=lambda c: c.pos[0]**2 + c.pos[1]**2)

    # Mostrar cada core
    for core in sorted_cores:
        temp = core.thermal.temp
        # Color según temperatura (caracteres)
        if temp < 35:
            temp_bar = "🟢" * int(temp/5) + "⚪" * (10 - int(temp/5))
        elif temp < 50:
            temp_bar = "🟡" * int((temp-30)/5) + "⚪" * (10 - int((temp-30)/5))
        else:
            temp_bar = "🔴" * min(10, int((temp-40)/5)) + "⚪" * max(0, 10 - int((temp-40)/5))

        proc_str = f"{core.current_process.name[:6]:6s}" if core.current_process else "IDLE  "
        hit_rate = core.hit_rate()
        ipc = core.ipc

        print(f"Core {core.id:2d} | {proc_str} | IPC:{ipc:4.2f} | Hit:{hit_rate*100:5.1f}% | "
              f"Temp:{temp:5.1f}°C {temp_bar} | Power:{core.thermal.power:6.2f}W")

    # Mapa de calor en 2D (posiciones)
    print("\n    Mapa de Calor (posición φ):")
    xs = [c.pos[0] for c in kernel.cores]
    ys = [c.pos[1] for c in kernel.cores]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    grid = [[' ' for _ in range(40)] for _ in range(20)]
    for core in kernel.cores:
        x = int((core.pos[0] - min_x) / (max_x - min_x) * 38) + 1
        y = int((core.pos[1] - min_y) / (max_y - min_y) * 18) + 1
        if 0 <= y < 20 and 0 <= x < 40:
            temp = core.thermal.temp
            if temp < 35:
                ch = '🟢'
            elif temp < 50:
                ch = '🟡'
            else:
                ch = '🔴'
            grid[y][x] = ch
    for row in grid:
        print('   ' + ''.join(row))

    print("\nφ-canales de refrigeración: 🌊🌊🌊 (flujo laminar no resonante)")
    print("-" * 70)

# =============================================================================
# 7. INICIALIZACIÓN (Disposición en Espiral Áurea φ)
# =============================================================================
def create_cores_phi(num_cores: int) -> List[CFDACore]:
    cores = []
    angle = math.pi * (3 - math.sqrt(5))  # ≈ 137.508°
    for i in range(num_cores):
        r = 2.0 * math.sqrt(i)
        theta = i * angle
        x = r * math.cos(theta)
        y = r * math.sin(theta)
        cores.append(CFDACore(i, (x, y)))
    return cores

def main():
    print("🌱 Inicializando CFDA_Gardener...")
    # Crear 16 cores en espiral φ
    cores = create_cores_phi(16)

    # Crear procesos con diferentes patrones de acceso
    processes = [
        Process(1, "Streamer", "stream", 0.9),
        Process(2, "StrideAI", "stride", 0.8),
        Process(3, "RandomIO", "random", 0.5),
        Process(4, "Tensor", "stride", 0.95),
        Process(5, "Logger", "stream", 0.3),
        Process(6, "Browser", "random", 0.6),
        Process(7, "DB", "random", 0.7),
        Process(8, "Render", "stream", 0.85),
    ]

    kernel = RelationalKernel(cores, processes)

    print("🚀 Ejecutando simulación (Ctrl+C para salir)...")
    time.sleep(1)

    cycle = 0
    try:
        while True:
            Y = kernel.step()
            if cycle % 5 == 0:  # renderizar cada 5 ciclos para no saturar
                render_garden(kernel, cycle)
            cycle += 1
            time.sleep(0.05)   # ~20 FPS
    except KeyboardInterrupt:
        print("\n\n🛑 Simulación detenida.")
        print("Estadísticas finales:")
        for core in kernel.cores:
            print(f"Core {core.id:2d}: Temp final={core.thermal.temp:.1f}°C, Hit rate={core.hit_rate()*100:.1f}%")

if __name__ == "__main__":
    main()