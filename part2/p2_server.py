#!/usr/bin/env python3
"""
Part 2 Server with BBR Congestion Control
Implements reliable file transfer over UDP with BBR algorithm
"""

import socket
import sys
import time
import struct
import os
from collections import deque
from enum import Enum

# Constants
MSS = 1180  # Maximum Segment Size (data payload)
HEADER_SIZE = 20
MAX_PACKET_SIZE = MSS + HEADER_SIZE
INITIAL_TIMEOUT = 1.0
MIN_TIMEOUT = 0.2
MAX_TIMEOUT = 3.0
ALPHA = 0.125
BETA = 0.25

class BBRMode(Enum):
    STARTUP = 1
    DRAIN = 2
    PROBE_BW = 3
    PROBE_RTT = 4

class BBRState:
    def __init__(self):
        # BBR core state
        self.mode = BBRMode.STARTUP
        self.btlbw = 0  # Bottleneck bandwidth (bytes/sec)
        self.rtprop = float('inf')  # Min RTT seen
        self.rtprop_stamp = time.time()
        self.rtprop_expired = False
        
        # Pacing and cwnd
        self.pacing_rate = 0
        self.cwnd = 1 * MSS  # Start with 1 MSS
        self.pacing_gain = 2.77  # High gain for startup
        self.cwnd_gain = 2.0
        
        # Cycle tracking for PROBE_BW
        self.cycle_index = 0
        self.cycle_stamp = time.time()
        self.probe_bw_gains = [1.25, 0.75, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        
        # Bandwidth probing
        self.btlbw_filter = deque(maxlen=10)  # Track recent bandwidth samples
        self.full_bw = 0
        self.full_bw_count = 0
        
        # RTT probing
        self.probe_rtt_done_stamp = 0
        self.probe_rtt_min_delay = float('inf')
        self.probe_rtt_min_stamp = 0
        
        # Delivery rate estimation
        self.delivered = 0
        self.delivered_time = time.time()
        
    def update_btlbw(self, rate):
        """Update bottleneck bandwidth estimate"""
        self.btlbw_filter.append(rate)
        if self.btlbw_filter:
            self.btlbw = max(self.btlbw_filter)
    
    def update_rtprop(self, rtt):
        """Update minimum RTT"""
        self.rtprop_expired = time.time() > self.rtprop_stamp + 10  # 10 sec expiration
        
        if rtt < self.rtprop or self.rtprop_expired:
            self.rtprop = rtt
            self.rtprop_stamp = time.time()
    
    def check_full_bw_reached(self):
        """Check if we've reached full bandwidth in startup"""
        if len(self.btlbw_filter) < 3:
            return False
        
        target = self.full_bw * 1.25
        if self.btlbw >= target:
            self.full_bw = self.btlbw
            self.full_bw_count = 0
            return False
        
        self.full_bw_count += 1
        return self.full_bw_count >= 3
    
    def enter_drain(self):
        """Enter drain mode"""
        self.mode = BBRMode.DRAIN
        self.pacing_gain = 1.0 / 2.77
        self.cwnd_gain = 2.0
    
    def enter_probe_bw(self):
        """Enter probe bandwidth mode"""
        self.mode = BBRMode.PROBE_BW
        self.pacing_gain = 1.0
        self.cwnd_gain = 2.0
        self.cycle_index = 0
        self.cycle_stamp = time.time()
    
    def enter_probe_rtt(self):
        """Enter probe RTT mode"""
        self.mode = BBRMode.PROBE_RTT
        self.pacing_gain = 1.0
        self.cwnd_gain = 1.0
        self.probe_rtt_done_stamp = 0
        self.probe_rtt_min_delay = float('inf')
    
    def update_pacing_rate(self):
        """Calculate pacing rate"""
        if self.rtprop == float('inf'):
            return
        
        bw = self.btlbw if self.btlbw > 0 else 10000  # Default 10KB/s
        self.pacing_rate = self.pacing_gain * bw
    
    def update_cwnd(self, bytes_in_flight):
        """Update congestion window"""
        if self.mode == BBRMode.PROBE_RTT:
            target_cwnd = 4 * MSS
        else:
            if self.rtprop == float('inf'):
                bdp = self.cwnd
            else:
                bdp = self.btlbw * self.rtprop
            target_cwnd = self.cwnd_gain * bdp
        
        target_cwnd = max(target_cwnd, 4 * MSS)
        
        if self.cwnd < target_cwnd:
            self.cwnd = min(self.cwnd + MSS, target_cwnd)
        elif self.cwnd > target_cwnd:
            self.cwnd = max(self.cwnd - MSS, target_cwnd)
    
    def advance_cycle(self):
        """Advance to next gain cycle in PROBE_BW"""
        self.cycle_index = (self.cycle_index + 1) % len(self.probe_bw_gains)
        self.pacing_gain = self.probe_bw_gains[self.cycle_index]
        self.cycle_stamp = time.time()


class ReliableUDPServer:
    def __init__(self, ip, port):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((ip, port))
        self.socket.settimeout(0.001)  # Non-blocking with short timeout
        
        self.ip = ip
        self.port = port
        
        # Sequence tracking
        self.seq_num = 0
        self.next_ack = 0
        
        # Window management
        self.send_base = 0
        self.window = {}  # seq -> (data, send_time)
        
        # RTT estimation
        self.estimated_rtt = INITIAL_TIMEOUT
        self.dev_rtt = 0
        self.rto = INITIAL_TIMEOUT
        
        # BBR state
        self.bbr = BBRState()
        
        # Statistics
        self.bytes_acked = 0
        self.last_ack_time = time.time()
        self.delivered_bytes = 0
        
    def create_packet(self, seq_num, data):
        """Create packet with header"""
        header = struct.pack('!I', seq_num) + b'\x00' * 16
        return header + data
    
    def parse_ack(self, packet):
        """Parse ACK packet"""
        if len(packet) < 4:
            return None
        ack_num = struct.unpack('!I', packet[:4])[0]
        return ack_num
    
    def update_rtt(self, sample_rtt):
        """Update RTT estimates"""
        if self.estimated_rtt == INITIAL_TIMEOUT:
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.dev_rtt = (1 - BETA) * self.dev_rtt + BETA * abs(sample_rtt - self.estimated_rtt)
            self.estimated_rtt = (1 - ALPHA) * self.estimated_rtt + ALPHA * sample_rtt
        
        self.rto = self.estimated_rtt + 4 * self.dev_rtt
        self.rto = max(MIN_TIMEOUT, min(MAX_TIMEOUT, self.rto))
        
        # Update BBR RTT
        self.bbr.update_rtprop(sample_rtt)
    
    def update_bbr_on_ack(self, bytes_acked, rtt_sample):
        """Update BBR state on ACK"""
        now = time.time()
        
        # Update delivery rate
        if bytes_acked > 0:
            time_elapsed = now - self.last_ack_time
            if time_elapsed > 0:
                delivery_rate = bytes_acked / time_elapsed
                self.bbr.update_btlbw(delivery_rate)
                self.delivered_bytes += bytes_acked
            self.last_ack_time = now
        
        # Update RTT
        if rtt_sample > 0:
            self.bbr.update_rtprop(rtt_sample)
        
        # State machine
        bytes_in_flight = len(self.window) * MSS
        
        if self.bbr.mode == BBRMode.STARTUP:
            if self.bbr.check_full_bw_reached():
                self.bbr.enter_drain()
        
        elif self.bbr.mode == BBRMode.DRAIN:
            if bytes_in_flight <= self.bbr.btlbw * self.bbr.rtprop:
                self.bbr.enter_probe_bw()
        
        elif self.bbr.mode == BBRMode.PROBE_BW:
            # Check if we should enter PROBE_RTT
            if self.bbr.rtprop_expired and not (self.bbr.mode == BBRMode.PROBE_RTT):
                self.bbr.enter_probe_rtt()
            else:
                # Advance gain cycle
                if now - self.bbr.cycle_stamp > self.bbr.rtprop:
                    self.bbr.advance_cycle()
        
        elif self.bbr.mode == BBRMode.PROBE_RTT:
            if self.bbr.probe_rtt_done_stamp == 0:
                if bytes_in_flight <= 4 * MSS:
                    self.bbr.probe_rtt_done_stamp = now + 0.2  # 200ms
            elif now > self.bbr.probe_rtt_done_stamp:
                self.bbr.rtprop_stamp = now
                self.bbr.enter_probe_bw()
        
        # Update pacing and cwnd
        self.bbr.update_pacing_rate()
        self.bbr.update_cwnd(bytes_in_flight)
    
    def should_send_packet(self):
        """Check if we can send a packet based on BBR cwnd"""
        bytes_in_flight = len(self.window) * MSS
        return bytes_in_flight < self.bbr.cwnd
    
    def get_pacing_delay(self):
        """Calculate delay between packets for pacing"""
        if self.bbr.pacing_rate > 0:
            return MSS / self.bbr.pacing_rate
        return 0.0
    
    def send_file(self, client_addr, filename):
        """Send file to client with BBR congestion control"""
        if not os.path.exists(filename):
            print(f"Error: File {filename} not found")
            return
        
        with open(filename, 'rb') as f:
            file_data = f.read()
        
        print(f"Sending file {filename} ({len(file_data)} bytes) to {client_addr}")
        
        # Split into chunks
        chunks = []
        for i in range(0, len(file_data), MSS):
            chunks.append(file_data[i:i+MSS])
        
        self.seq_num = 0
        self.send_base = 0
        self.next_ack = 0
        self.window.clear()
        
        last_send_time = time.time()
        retransmit_count = 0
        
        while self.send_base < len(chunks):
            now = time.time()
            
            # Send new packets if window allows
            while self.seq_num < len(chunks) and self.should_send_packet():
                if self.seq_num not in self.window:
                    # Pacing delay
                    pacing_delay = self.get_pacing_delay()
                    if now - last_send_time < pacing_delay:
                        time.sleep(pacing_delay - (now - last_send_time))
                    
                    packet = self.create_packet(self.seq_num, chunks[self.seq_num])
                    self.socket.sendto(packet, client_addr)
                    self.window[self.seq_num] = (packet, time.time())
                    last_send_time = time.time()
                    self.seq_num += 1
            
            # Check for ACKs
            try:
                ack_packet, _ = self.socket.recvfrom(1024)
                ack_num = self.parse_ack(ack_packet)
                
                if ack_num is not None and ack_num > self.send_base:
                    # Calculate RTT for this ACK
                    if self.send_base in self.window:
                        send_time = self.window[self.send_base][1]
                        rtt_sample = time.time() - send_time
                        self.update_rtt(rtt_sample)
                    else:
                        rtt_sample = 0
                    
                    # Calculate bytes acked
                    bytes_acked = (ack_num - self.send_base) * MSS
                    
                    # Update BBR
                    self.update_bbr_on_ack(bytes_acked, rtt_sample)
                    
                    # Remove acked packets from window
                    for seq in range(self.send_base, ack_num):
                        if seq in self.window:
                            del self.window[seq]
                    
                    self.send_base = ack_num
                    self.next_ack = ack_num
                    
                    if self.send_base % 100 == 0:
                        print(f"Progress: {self.send_base}/{len(chunks)} packets, "
                              f"cwnd: {self.bbr.cwnd/MSS:.1f} MSS, "
                              f"mode: {self.bbr.mode.name}, "
                              f"RTT: {self.estimated_rtt*1000:.1f}ms")
            
            except socket.timeout:
                pass
            
            # Check for timeouts and retransmit
            now = time.time()
            for seq in list(self.window.keys()):
                packet, send_time = self.window[seq]
                if now - send_time > self.rto:
                    # Timeout - retransmit
                    self.socket.sendto(packet, client_addr)
                    self.window[seq] = (packet, now)
                    retransmit_count += 1
                    
                    # On timeout, BBR should reduce cwnd
                    self.bbr.cwnd = max(4 * MSS, self.bbr.cwnd * 0.5)
                    
                    if retransmit_count % 10 == 0:
                        print(f"Timeout: retransmitting seq {seq}, retrans: {retransmit_count}")
        
        # Send EOF
        eof_packet = self.create_packet(len(chunks), b"EOF")
        for _ in range(5):
            self.socket.sendto(eof_packet, client_addr)
            time.sleep(0.1)
        
        print(f"File transfer complete. Retransmissions: {retransmit_count}")
        print(f"Final cwnd: {self.bbr.cwnd/MSS:.1f} MSS, "
              f"Final BW estimate: {self.bbr.btlbw/1000000:.2f} Mbps, "
              f"Min RTT: {self.bbr.rtprop*1000:.1f}ms")
    
    def run(self):
        """Main server loop"""
        print(f"Server listening on {self.ip}:{self.port}")
        print("Waiting for client request...")
        
        # Wait for initial request
        data, client_addr = self.socket.recvfrom(1024)
        print(f"Received request from {client_addr}")
        
        # Send file
        self.send_file(client_addr, "data.txt")
        
        print("Server finished")


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 p2_server.py <SERVER_IP> <SERVER_PORT>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    
    server = ReliableUDPServer(server_ip, server_port)
    server.run()


if __name__ == "__main__":
    main()
