#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CFDA_Gardener_v3 - Modelo Térmico con Refrigeración Consistente
Arquitectura modular: 
- ThermalModel acoplado a refrigerante (conserva energía).
- Red de canales fractales con flujo y balance de calor.
- Comparativa A/B: none, spiral, fractal, hybrid.
"""

import random
import time
import math
import sys
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Set
from collections import deque

# =============================================================================
# CONSTANTES FÍSICAS (aproximadas para microcanales)
# =============================================================================
H_CONV = 10000.0         # Coeficiente de convección agua-silicio [W/m²K]
CP_WATER = 4186.0        # Capacidad calorífica agua [J/kgK]
RHO_WATER = 1000.0       # Densidad agua [kg/m³]
CHANNEL_DIAMETER = 10e-6 # Diámetro microcanal [m] (10 µm)
CORE_SIZE = 500e-6       # Distancia típica entre cores [m] (500 µm)
INLET_TEMP = 20.0        # Temperatura de entrada del refrigerante [°C]
FLOW_VELOCITY = 0.5      # Velocidad media del refrigerante [m/s] (estimada)

# =============================================================================
# 1. MODELO TÉRMICO CON REFRIGERACIÓN ACTIVA
# =============================================================================
class ThermalModel:
    """
    Modelo RC con acoplamiento a refrigerante local.
    Balance de energía: dT = (P - (T-Tamb)/R_amb - G_cool*(T - T_cool)) * dt / C
    """
    def __init__(self, ambient_temp=25.0, thermal_resistance=2.0, heat_capacity=5.0):
        self.temp = ambient_temp
        self.ambient = ambient_temp
        self.R_ambient = thermal_resistance   # °C/W al ambiente (disipador trasero)
        self.C = heat_capacity                # J/°C
        self.power = 0.0
        # Acoplamiento al refrigerante
        self.G_cool = 0.0                     # Conductancia térmica al refrigerante [W/K]
        self.coolant_temp = ambient_temp       # Temperatura local del refrigerante [°C]

    def set_coolant_coupling(self, G_cool: float, coolant_temp: float):
        self.G_cool = G_cool
        self.coolant_temp = coolant_temp

    def update(self, energy_nj: float, dt: float):
        """energy_nj: nanojulios, dt: segundos. Retorna calor cedido al refrigerante [J]."""
        power = energy_nj * 1e-9 / dt
        self.power = power
        heat_in = power * dt

        # Disipación al ambiente
        heat_out_ambient = (self.temp - self.ambient) / self.R_ambient * dt

        # Disipación al refrigerante (nuevo término)
        heat_out_coolant = self.G_cool * (self.temp - self.coolant_temp) * dt
        # Limitar: no puede ceder más calor del que tiene
        # (evita inestabilidades si G_cool es muy grande)
        max_heat_out = heat_in + (self.temp - self.ambient) * self.C / dt
        if heat_out_coolant > max_heat_out:
            heat_out_coolant = max_heat_out

        self.temp += (heat_in - heat_out_ambient - heat_out_coolant) / self.C
        return heat_out_coolant  # [J] para pasar al refrigerante


# =============================================================================
# 2. RED DE CANALES FRACTALES (con flujo y balance de energía)
# =============================================================================
class ChannelSegment:
    """Segmento de canal con propiedades térmicas y de flujo."""
    def __init__(self, start_pos, end_pos, radius: float, parent=None):
        self.start = start_pos
        self.end = end_pos
        self.radius = radius          # radio hidráulico [m]
        self.length = math.sqrt((end_pos[0]-start_pos[0])**2 + (end_pos[1]-start_pos[1])**2) * 1e-6  # asumiendo unidades µm -> m
        self.volume = math.pi * radius**2 * self.length  # m³
        self.mass = RHO_WATER * self.volume  # kg de agua en el segmento
        self.flow_rate = 0.0          # caudal másico [kg/s] asignado según Murray
        self.coolant_temp = INLET_TEMP  # temperatura del agua en este segmento
        self.connected_cores = []     # cores en contacto térmico
        self.G_per_core = []          # conductancia para cada core conectado [W/K]

    def add_core(self, core, G_cool: float):
        self.connected_cores.append(core)
        self.G_per_core.append(G_cool)
        # Configurar el modelo térmico del core
        core.thermal.set_coolant_coupling(G_cool, self.coolant_temp)

    def update_coolant_temp(self, total_heat: float, dt: float):
        """
        total_heat: calor total [J] cedido por los cores a este segmento en este paso.
        El refrigerante se calienta con ese calor, y además hay flujo entrante/saliente.
        Modelo simplificado: suponemos flujo pistón -> entra refrigerante a T_inlet, sale a T_actual.
        Balance: d(m*T)/dt = m_dot_in * Cp * T_inlet - m_dot_out * Cp * T_actual + Q_cores
        Si m_dot_in = m_dot_out = m_dot:
            m * Cp * dT/dt = m_dot * Cp * (T_inlet - T) + Q
        Aproximamos con Euler explícito:
            dT = (m_dot * (T_inlet - T) + Q/Cp) * dt / m
        """
        if self.mass > 0 and self.flow_rate > 0:
            m_dot = self.flow_rate
            Q_dot = total_heat / dt if dt > 0 else 0.0  # potencia térmica [W]
            dT = (m_dot * (INLET_TEMP - self.coolant_temp) + Q_dot / CP_WATER) * dt / self.mass
            self.coolant_temp += dT
        else:
            # Sin flujo, solo se calienta
            if self.mass > 0:
                self.coolant_temp += total_heat / (self.mass * CP_WATER)
        # Actualizar temperatura del refrigerante para los cores
        for core in self.connected_cores:
            core.thermal.coolant_temp = self.coolant_temp


class FractalCoolingNetwork:
    """Genera y gestiona la red de canales fractales."""
    def __init__(self, cores: List, pattern: str = "hybrid"):
        self.cores = cores
        self.pattern = pattern
        self.segments = []  # lista de ChannelSegment
        self._build_network()

    def _create_segment(self, start, end, radius, parent=None):
        seg = ChannelSegment(start, end, radius, parent)
        self.segments.append(seg)
        return seg

    def _assign_flow_rates(self):
        """Distribuye caudal total según radios (Murray: caudal ∝ r³)."""
        # Caudal total de entrada (kg/s) estimado por bomba
        total_flow = RHO_WATER * math.pi * (CHANNEL_DIAMETER/2)**2 * FLOW_VELOCITY * 4  # 4 canales de entrada aprox.
        # Asignar proporcional a r^3 a cada segmento hoja (sin hijos)
        # Simplificación: asignamos a todos los segmentos según su radio
        sum_r3 = sum(s.radius**3 for s in self.segments)
        for seg in self.segments:
            seg.flow_rate = total_flow * (seg.radius**3) / sum_r3 if sum_r3 > 0 else 0.0

    def _build_network(self):
        if self.pattern == "spiral":
            self._build_spiral_channels()
        elif self.pattern == "fractal":
            self._build_murray_tree()
        else:  # hybrid
            self._build_hybrid()
        self._assign_flow_rates()
        self._connect_cores_to_segments()

    def _connect_cores_to_segments(self):
        """Cada core se asocia al segmento más cercano."""
        for core in self.cores:
            min_dist = float('inf')
            closest = None
            for seg in self.segments:
                # Distancia del core al punto medio del segmento
                mx = (seg.start[0] + seg.end[0]) / 2
                my = (seg.start[1] + seg.end[1]) / 2
                d = (core.pos[0]-mx)**2 + (core.pos[1]-my)**2
                if d < min_dist:
                    min_dist = d
                    closest = seg
            if closest:
                # Calcular conductancia: G = h * A_contacto
                # A_contacto = área de la pared del canal en contacto con el core
                # Asumimos que el core "toca" una longitud del canal igual a CORE_SIZE
                A_contact = math.pi * closest.radius * 2 * CORE_SIZE  # área lateral simplificada
                G_cool = H_CONV * A_contact
                closest.add_core(core, G_cool)

    def _build_spiral_channels(self):
        sorted_cores = sorted(self.cores, 
                             key=lambda c: math.atan2(c.pos[1], c.pos[0]) + 
                                          math.sqrt(c.pos[0]**2 + c.pos[1]**2) * 0.01)
        for i in range(len(sorted_cores) - 1):
            c1, c2 = sorted_cores[i], sorted_cores[i+1]
            seg = self._create_segment(c1.pos, c2.pos, CHANNEL_DIAMETER/2)
            # No añadimos cores aquí, se hará en _connect_cores_to_segments

    def _build_murray_tree(self):
        center_x = sum(c.pos[0] for c in self.cores) / len(self.cores)
        center_y = sum(c.pos[1] for c in self.cores) / len(self.cores)
        sorted_cores = sorted(self.cores, 
                             key=lambda c: (c.pos[0]-center_x)**2 + (c.pos[1]-center_y)**2)
        self._recursive_branch(sorted_cores, (center_x, center_y), CHANNEL_DIAMETER/2 * 4)  # tronco más grueso

    def _recursive_branch(self, cores_subset, branch_point, radius):
        if len(cores_subset) <= 2:
            for core in cores_subset:
                seg = self._create_segment(branch_point, core.pos, radius)
            return
        sorted_by_angle = sorted(cores_subset, 
                                key=lambda c: math.atan2(c.pos[1]-branch_point[1], c.pos[0]-branch_point[0]))
        mid = len(sorted_by_angle) // 2
        group1 = sorted_by_angle[:mid]
        group2 = sorted_by_angle[mid:]
        mid1 = (sum(c.pos[0] for c in group1)/len(group1), sum(c.pos[1] for c in group1)/len(group1))
        mid2 = (sum(c.pos[0] for c in group2)/len(group2), sum(c.pos[1] for c in group2)/len(group2))
        # Ley de Murray: r_padre³ = r_hijo1³ + r_hijo2³
        r1 = radius * (len(group1) / len(cores_subset)) ** (1/3)
        r2 = radius * (len(group2) / len(cores_subset)) ** (1/3)
        self._create_segment(branch_point, mid1, r1)
        self._create_segment(branch_point, mid2, r2)
        self._recursive_branch(group1, mid1, r1)
        self._recursive_branch(group2, mid2, r2)

    def _build_hybrid(self):
        sorted_cores = sorted(self.cores, key=lambda c: math.atan2(c.pos[1], c.pos[0]))
        n_branches = 4
        cores_per_branch = max(1, len(self.cores) // n_branches)
        prev_bx, prev_by = None, None
        for i in range(n_branches):
            start_idx = i * cores_per_branch
            end_idx = start_idx + cores_per_branch if i < n_branches-1 else len(self.cores)
            branch_cores = sorted_cores[start_idx:end_idx]
            if not branch_cores:
                continue
            bx = sum(c.pos[0] for c in branch_cores) / len(branch_cores)
            by = sum(c.pos[1] for c in branch_cores) / len(branch_cores)
            # Tronco espiral entre puntos de ramificación
            if prev_bx is not None:
                self._create_segment((prev_bx, prev_by), (bx, by), CHANNEL_DIAMETER/2 * 2)  # tronco más grueso
            # Ramas fractales desde punto de ramificación a cada core
            for core in branch_cores:
                self._create_segment((bx, by), core.pos, CHANNEL_DIAMETER/2)
            prev_bx, prev_by = bx, by

    def update_network(self, dt: float):
        """Recolecta calor de los cores y actualiza temperaturas de refrigerante."""
        # Inicializar calor acumulado por segmento
        heat_per_segment = {seg: 0.0 for seg in self.segments}
        # Ejecutar actualización de cores (ya se hizo en el kernel)
        # Pero aquí recogemos el heat_out_coolant que el core ya calculó.
        # Para eso, necesitamos que el core almacene el último Q_coolant.
        # Modificaremos ThermalModel.update para que devuelva Q_coolant, y lo almacenamos.
        # En el kernel, llamaremos a core.thermal.update y guardaremos el Q en core._last_Q_cool.
        for core in self.cores:
            if hasattr(core, '_last_Q_cool'):
                Q = core._last_Q_cool
                # Encontrar el segmento asociado (el más cercano)
                # Ya tenemos la conexión via add_core; podemos buscar el segmento que tiene a este core
                for seg in self.segments:
                    if core in seg.connected_cores:
                        heat_per_segment[seg] += Q
                        break
        # Actualizar cada segmento
        for seg, Q_total in heat_per_segment.items():
            seg.update_coolant_temp(Q_total, dt)
            # Actualizar coolant_temp en los cores asociados
            for core in seg.connected_cores:
                core.thermal.coolant_temp = seg.coolant_temp


# =============================================================================
# 3. PREDICTOR CFDA (sin cambios)
# =============================================================================
class CFDAPredictor:
    def __init__(self):
        self.history = []
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
        s1 = self.history[-1] - self.history[-2]
        s2 = self.history[-2] - self.history[-3]
        if s1 == s2 and s1 != 0:
            self.correct += 1
            return addr + s1
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

class CFDACore:
    def __init__(self, core_id: int, position):
        self.id = core_id
        self.pos = position
        self.predictor = CFDAPredictor()
        self.local_buffer = {}
        self.buffer_size = 64
        self.accesses = 0
        self.hits = 0
        self.thermal = ThermalModel()
        self.current_process = None
        self.ipc = 0.0
        self._last_Q_cool = 0.0

    def access(self, addr: int) -> float:
        self.accesses += 1
        self.predictor.update_markov()
        pred = self.predictor.predict(addr)
        if pred is not None:
            self._prefetch(pred)
        line = addr // 64
        if line in self.local_buffer:
            self.hits += 1
            energy = 0.5
            lat = 1
        else:
            self._load_line(line)
            energy = 50.0
            lat = 25
        # Actualizar modelo térmico y guardar calor cedido al refrigerante
        Q_cool = self.thermal.update(energy, dt=0.5e-9)
        self._last_Q_cool = Q_cool
        self.ipc = 1.0 / lat if lat > 0 else 1.0
        return energy

    def _prefetch(self, addr):
        line = addr // 64
        if len(self.local_buffer) < self.buffer_size:
            self.local_buffer[line] = True

    def _load_line(self, line):
        if len(self.local_buffer) >= self.buffer_size:
            self.local_buffer.pop(next(iter(self.local_buffer)))
        self.local_buffer[line] = True

    def hit_rate(self):
        return self.hits / self.accesses if self.accesses > 0 else 0.0

class Process:
    def __init__(self, pid, name, pattern, intensity):
        self.pid = pid
        self.name = name
        self.pattern = pattern
        self.intensity = intensity
        self.addr = random.randint(0x1000, 0x10000)
        self.stride = random.choice([64, 128, 256, -128])
        self.progress = 0
        self.assigned_core = None

    def next_address(self):
        if random.random() > self.intensity:
            return None
        if self.pattern == "stream":
            self.addr += 64
        elif self.pattern == "stride":
            self.addr += self.stride
        else:
            self.addr = random.randint(0x1000, 0x100000)
        self.progress += 1
        return self.addr

class RelationalKernel:
    def __init__(self, cores, processes, cooling_network=None):
        self.cores = cores
        self.processes = processes
        self.cooling_network = cooling_network
        self.history = deque(maxlen=100)

    def schedule(self):
        hungry = sorted(self.processes, key=lambda p: -p.intensity)
        cool_cores = sorted(self.cores, key=lambda c: c.thermal.temp)
        for p in hungry:
            if p.assigned_core is not None:
                continue
            for core in cool_cores:
                if core.current_process is None:
                    core.current_process = p
                    p.assigned_core = core
                    break

    def step(self):
        dt = 0.5e-9  # 0.5 ns por ciclo
        # 1. Ejecutar accesos en cores ocupados (esto ya actualiza sus térmicos)
        for core in self.cores:
            if core.current_process is not None:
                proc = core.current_process
                addr = proc.next_address()
                if addr is not None:
                    energy = core.access(addr)
                else:
                    core.thermal.update(0.1, dt)
                    core._last_Q_cool = 0.0
                if proc.progress > 200:
                    core.current_process = None
                    proc.assigned_core = None
                    proc.progress = 0
            else:
                core.thermal.update(0.05, dt)
                core._last_Q_cool = 0.0

        # 2. Actualizar red de refrigeración (usa _last_Q_cool de los cores)
        if self.cooling_network:
            self.cooling_network.update_network(dt)

        # 3. Reasignar procesos
        self.schedule()

        # 4. Calcular coherencia
        Y = self.compute_coherence()
        self.history.append(Y)
        return Y

    def compute_coherence(self):
        total = 0.0
        for core in self.cores:
            if core.thermal.temp > 0:
                total += core.ipc / (core.thermal.temp / 30.0)
        return total / len(self.cores)

# =============================================================================
# VISUALIZACIÓN (opcional, simplificada para resultados)
# =============================================================================
def render_garden(kernel, cycle):
    # Versión reducida para no saturar, solo imprime resumen
    pass  # Podemos omitir la visualización ASCII para centrarnos en resultados.

# =============================================================================
# FUNCIÓN PRINCIPAL DE COMPARATIVA
# =============================================================================
def create_cores_phi(num_cores):
    cores = []
    angle = math.pi * (3 - math.sqrt(5))
    for i in range(num_cores):
        r = 2.0 * math.sqrt(i)
        theta = i * angle
        x = r * math.cos(theta)
        y = r * math.sin(theta)
        cores.append(CFDACore(i, (x, y)))
    return cores

def run_comparison():
    print("🌱 Experimento: Comparación de Redes de Refrigeración (Modelo Corregido)\n")
    patterns = ["none", "spiral", "fractal", "hybrid"]
    results = {}
    num_cores = 16
    num_cycles = 100  # más ciclos para ver estabilización

    for pattern in patterns:
        print(f"\n{'='*60}")
        print(f"Probando: {pattern.upper()}")
        print('='*60)
        cores = create_cores_phi(num_cores)
        processes = [
            Process(1, "Streamer", "stream", 0.9),
            Process(2, "StrideAI", "stride", 0.8),
            Process(3, "RandomIO", "random", 0.5),
            Process(4, "Tensor", "stride", 0.95),
        ]
        cooling = None
        if pattern != "none":
            cooling = FractalCoolingNetwork(cores, pattern=pattern)
        kernel = RelationalKernel(cores, processes, cooling)

        temps_history = []
        for cycle in range(num_cycles):
            Y = kernel.step()
            max_temp = max(c.thermal.temp for c in cores)
            temps_history.append(max_temp)

        final_temps = [c.thermal.temp for c in cores]
        avg_temp = sum(final_temps) / len(final_temps)
        temp_variance = sum((t - avg_temp)**2 for t in final_temps) / len(final_temps)
        results[pattern] = {
            'avg_temp': avg_temp,
            'variance': temp_variance,
            'Y_final': Y,
            'max_temp': max(final_temps),
            'min_temp': min(final_temps)
        }
        print(f"Resultados tras {num_cycles} ciclos:")
        print(f"  Temp prom: {avg_temp:.2f}°C, Max: {max(final_temps):.2f}°C, Min: {min(final_temps):.2f}°C")
        print(f"  Varianza térmica: {temp_variance:.4f}")
        print(f"  Coherencia Y(t): {Y:.4f}")

    # Resumen
    print(f"\n\n{'='*60}")
    print("RESUMEN COMPARATIVO (Modelo con refrigeración activa)")
    print('='*60)
    print(f"{'Patrón':<12} {'T prom':>8} {'Varianza':>10} {'Y(t)':>8} {'Max T':>8}")
    print('-'*50)
    for p in patterns:
        r = results[p]
        print(f"{p:<12} {r['avg_temp']:8.2f} {r['variance']:10.4f} {r['Y_final']:8.4f} {r['max_temp']:8.2f}")
    return results

if __name__ == "__main__":
    run_comparison()
