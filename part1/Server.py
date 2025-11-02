#!/usr/bin/env python3

import socket
import sys
import time
import struct
import threading
from collections import defaultdict

class ReliableUDPServer:
    def __init__(self, server_ip, server_port, sws):
        self.server_ip = server_ip
        self.server_port = server_port
        self.sws = sws  # Sender window size in packets
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.server_ip, self.server_port))
        
        # Packet management
        self.all_packets = {}  # {seq_num: (packet_data, timestamp)}
        self.acked_packets = set()  # Set of acknowledged packet numbers
        
        # SACK-specific
        self.sack_ranges = []  # List of (start, length) tuples
        self.next_expected = 0  # Next expected packet from client perspective
        
        # Timeout parameters
        self.estimated_rtt = 0.5
        self.dev_rtt = 0.25
        self.timeout_interval = 0.8
        self.min_timeout = 0.2
        self.max_timeout = 2.5
        self.alpha = 0.125
        self.beta = 0.25
        
        # Fast retransmit optimization
        self.ahead_acked_count = defaultdict(int)
        self.urgent_retransmit = set()
        
        self.lock = threading.Lock()
        self.running = True
        self.eof_acked = False
        self.client_addr = None
        
    def make_packet(self, seq_num, data):
        """Create packet with header"""
        header = struct.pack('!I', seq_num) + b'\x00' * 16
        return header + data
    
    def parse_sack(self, packet):
        """Parse SACK packet.
        
        Returns:
            (next_expected, sack_ranges) where sack_ranges is [(start, length), ...]
        """
        if len(packet) < 20:
            return None, []
        
        next_expected = struct.unpack('!I', packet[:4])[0]
        
        # Parse SACK ranges from bytes 4-19 (up to 4 ranges)
        sack_ranges = []
        for i in range(4, 20, 4):
            if i + 4 <= len(packet):
                start, length = struct.unpack('!HH', packet[i:i+4])
                if start != 0 or length != 0:  # Non-zero range
                    sack_ranges.append((start, length))
        
        return next_expected, sack_ranges
    
    def update_rtt(self, sample_rtt):
        """Update RTO based on measured RTT"""
        if self.estimated_rtt == 0.5:  # First measurement
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.estimated_rtt = (1 - self.alpha) * self.estimated_rtt + self.alpha * sample_rtt
            self.dev_rtt = (1 - self.beta) * self.dev_rtt + self.beta * abs(sample_rtt - self.estimated_rtt)
        
        self.timeout_interval = self.estimated_rtt + 3 * self.dev_rtt
        self.timeout_interval = max(self.min_timeout, min(self.timeout_interval, self.max_timeout))
    
    def update_acked_from_sack_ranges(self, sack_ranges, total_packets):
        """Efficiently update acked packets from SACK ranges"""
        new_acks = set()
        for start, length in sack_ranges:
            end = min(start + length, total_packets)
            for seq in range(start, end):
                if seq not in self.acked_packets:
                    self.acked_packets.add(seq)
                    new_acks.add(seq)
        return new_acks
    
    def process_fast_retransmit(self, new_acks):
        """Efficient fast retransmit: O(n*k) where k is lookback window"""
        LOOKBACK_WINDOW = 20  # Only check 20 packets behind each new ACK
        
        for acked_seq in sorted(new_acks):
            # Look back only LOOKBACK_WINDOW packets from this ack
            start_seq = max(0, acked_seq - LOOKBACK_WINDOW)
            
            # Find first unacked packet in this window
            for prior_seq in range(start_seq, acked_seq):
                if prior_seq not in self.acked_packets:
                    self.ahead_acked_count[prior_seq] += 1
                    
                    # Trigger urgent retransmit after 3 packets acked ahead of it
                    if self.ahead_acked_count[prior_seq] >= 3:
                        if prior_seq not in self.urgent_retransmit:
                            self.urgent_retransmit.add(prior_seq)
                        self.ahead_acked_count[prior_seq] = 0
                    
                    # Only increment for first missing packet in window
                    break
    
    def get_missing_packets_prioritized(self, total_packets):
        """Get missing packets prioritized by proximity to next_expected"""
        with self.lock:
            # Separate urgent (fast retransmit) and normal missing packets
            urgent = []
            near_expected = []  # Close to next_expected
            far_missing = []    # Far from next_expected
            
            NEAR_THRESHOLD = self.sws * 2  # Define "near" as 2x window size
            
            for seq in range(total_packets):
                if seq not in self.acked_packets:
                    if seq in self.urgent_retransmit:
                        urgent.append(seq)
                    elif seq < self.next_expected + NEAR_THRESHOLD:
                        near_expected.append(seq)
                    else:
                        far_missing.append(seq)
            
            # Priority: urgent first, then near expected, then far
            return urgent + near_expected + far_missing
    
    def send_data(self, data_bytes):
        """Send data using SACK-based protocol"""
        chunk_size = 1180
        chunks = [data_bytes[i:i+chunk_size] for i in range(0, len(data_bytes), chunk_size)]
        total_packets = len(chunks)
        
        print(f"Sending {len(data_bytes)} bytes in {total_packets} packets")
        print(f"Window size: {self.sws} packets")
        
        # Prepare all packets
        for seq, chunk in enumerate(chunks):
            packet = self.make_packet(seq, chunk)
            self.all_packets[seq] = (packet, None)
        
        # Start SACK receiver thread
        sack_thread = threading.Thread(target=self.receive_sacks, args=(total_packets,))
        sack_thread.daemon = True
        sack_thread.start()
        
        # Main sending loop
        last_progress_report = time.time()
        
        while True:
            with self.lock:
                # Check if all packets are acknowledged
                if len(self.acked_packets) >= total_packets:
                    print("All packets acknowledged via SACK")
                    break
                

            # Get prioritized list of missing packets
            missing_packets = self.get_missing_packets_prioritized(total_packets)
            
            # Send missing packets within window
            packets_sent = 0
            current_time = time.time()
            
            for seq in missing_packets[:self.sws]:
                urgent = False
                
                # Check if this is an urgent retransmit
                with self.lock:
                    if seq in self.urgent_retransmit:
                        urgent = True
                        self.urgent_retransmit.remove(seq)
                
                # Apply timeout check for non-urgent packets
                if not urgent:
                    with self.lock:
                        _, send_time = self.all_packets[seq]
                        if send_time is not None and (current_time - send_time) < self.timeout_interval:
                            continue
                
                # Send packet
                with self.lock:
                    packet, _ = self.all_packets[seq]
                
                try:
                    self.sock.sendto(packet, self.client_addr)
                    
                    with self.lock:
                        self.all_packets[seq] = (packet, current_time)
                        # Reset ahead ack counter after retransmit
                        self.ahead_acked_count[seq] = 0
                    
                    packets_sent += 1
                    if packets_sent >= self.sws:
                        break
                        
                except Exception as e:
                    print(f"Error sending packet {seq}: {e}")
                    continue
            
            # Small delay to avoid busy waiting
            time.sleep(0.001)
        
        print("Data transfer complete")
    
    def receive_sacks(self, total_packets):
        """Receive and process SACK packets"""
        self.sock.settimeout(0.05)
        
        while self.running:
            try:
                data, addr = self.sock.recvfrom(1200)
                
                # Check for EOF ACK
                if len(data) >= 4:
                    ack_value = struct.unpack('!I', data[:4])[0]
                    if ack_value == 0xFFFFFFFE:
                        with self.lock:
                            self.eof_acked = True
                        print("Received EOF ACK")
                        continue
                
                # Parse SACK
                next_expected, sack_ranges = self.parse_sack(data)
                if next_expected is None:
                    continue
                
                with self.lock:
                    old_next_expected = self.next_expected
                    self.next_expected = max(self.next_expected, next_expected)
                    
                    new_acks = set()
                    
                    # Process cumulative ACK
                    for seq in range(old_next_expected, self.next_expected):
                        if seq < total_packets and seq not in self.acked_packets:
                            self.acked_packets.add(seq)
                            new_acks.add(seq)
                    
                    # Process SACK ranges
                    self.sack_ranges = sack_ranges
                    sack_new_acks = self.update_acked_from_sack_ranges(sack_ranges, total_packets)
                    new_acks.update(sack_new_acks)
                    
                    # Process fast retransmit detection
                    if new_acks:
                        self.process_fast_retransmit(new_acks)
                    
                    # Update RTT based on cumulative ACK advancement
                    if self.next_expected > old_next_expected:
                        for seq in range(old_next_expected, self.next_expected):
                            if seq in self.all_packets and self.all_packets[seq][1]:
                                sample_rtt = time.time() - self.all_packets[seq][1]
                                self.update_rtt(sample_rtt)
                                break
            
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"Error receiving SACK: {e}")
                break
    
    def send_eof(self):
        """Send EOF and wait for ACK"""
        eof_packet = self.make_packet(0xFFFFFFFF, b'EOF')
        max_attempts = 10
        attempt = 0
        
        print("Sending EOF...")
        
        while not self.eof_acked and attempt < max_attempts:
            self.sock.sendto(eof_packet, self.client_addr)
            time.sleep(0.2)
            attempt += 1
        
        if self.eof_acked:
            print("EOF acknowledged")
            return True
        else:
            print("EOF not acknowledged")
            return False
    
    def run(self):
        """Main server loop"""
        print(f"Server listening on {self.server_ip}:{self.server_port}")
        print(f"Window size: {self.sws} packets ({self.sws * 1180} bytes)")
        
        try:
            # Wait for client request
            data, self.client_addr = self.sock.recvfrom(1200)
            print(f"Received request from {self.client_addr}")
            
            # Read file
            try:
                with open('data.txt', 'rb') as f:
                    file_data = f.read()
                print(f"File size: {len(file_data)} bytes")
            except FileNotFoundError:
                print("Error: data.txt not found")
                return
            
            # Send file
            start_time = time.time()
            self.send_data(file_data)
            end_time = time.time()
            
            transfer_time = end_time - start_time
            throughput = (len(file_data) * 8) / transfer_time / 1e6 if transfer_time > 0 else 0
            
            print(f"Transfer time: {transfer_time:.2f} seconds")
            print(f"Throughput: {throughput:.2f} Mbps")
            
            # Send EOF
            self.send_eof()
            
        except KeyboardInterrupt:
            print("\nServer interrupted")
        finally:
            self.running = False
            time.sleep(0.2)
            self.sock.close()
            print("Server closed")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 p1_server.py <SERVER_IP> <SERVER_PORT> <SWS>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    sws = int(sys.argv[3])
    
    # Convert SWS from bytes to packets (assuming 1180 bytes per packet)
    sws_packets = max(1, sws // 1180)
    
    server = ReliableUDPServer(server_ip, server_port, sws_packets)
    server.run()