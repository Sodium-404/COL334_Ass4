#!/usr/bin/env python3
"""
Part 2 Client with Reliable UDP Reception
Receives file from server and sends ACKs
"""

import socket
import sys
import time
import struct
import os

# Constants
MSS = 1180
HEADER_SIZE = 20
MAX_PACKET_SIZE = MSS + HEADER_SIZE
MAX_RETRIES = 5
RETRY_TIMEOUT = 2.0

class ReliableUDPClient:
    def __init__(self, server_ip, server_port, prefix):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.settimeout(0.1)  # 100ms timeout for receiving
        
        self.server_ip = server_ip
        self.server_port = int(server_port)
        self.prefix = prefix
        
        # Reception state
        self.expected_seq = 0
        self.received_data = {}  # seq -> data (for out-of-order packets)
        self.max_seq_received = -1
        
        # Statistics
        self.total_packets = 0
        self.duplicate_packets = 0
        self.out_of_order_packets = 0
        
    def parse_packet(self, packet):
        """Parse received packet"""
        if len(packet) < HEADER_SIZE:
            return None, None
        
        seq_num = struct.unpack('!I', packet[:4])[0]
        data = packet[HEADER_SIZE:]
        return seq_num, data
    
    def create_ack(self, ack_num):
        """Create ACK packet"""
        return struct.pack('!I', ack_num) + b'\x00' * 16
    
    def send_ack(self, ack_num):
        """Send ACK to server"""
        ack_packet = self.create_ack(ack_num)
        self.socket.sendto(ack_packet, (self.server_ip, self.server_port))
    
    def send_request(self):
        """Send initial file request to server"""
        print(f"Sending request to {self.server_ip}:{self.server_port}")
        
        for attempt in range(MAX_RETRIES):
            try:
                # Send request
                self.socket.sendto(b'\x01', (self.server_ip, self.server_port))
                print(f"Request sent (attempt {attempt + 1}/{MAX_RETRIES})")
                
                # Wait for first packet
                self.socket.settimeout(RETRY_TIMEOUT)
                packet, _ = self.socket.recvfrom(MAX_PACKET_SIZE + 100)
                
                if packet:
                    print("Server responded, starting file transfer...")
                    self.socket.settimeout(0.1)
                    return packet
                
            except socket.timeout:
                print(f"Timeout on attempt {attempt + 1}")
                if attempt == MAX_RETRIES - 1:
                    print("Failed to connect to server after maximum retries")
                    return None
        
        return None
    
    def receive_file(self, output_filename):
        """Receive file from server"""
        print(f"Starting file reception, output: {output_filename}")
        
        # Send request and get first packet
        first_packet = self.send_request()
        if first_packet is None:
            return False
        
        # Process first packet
        seq_num, data = self.parse_packet(first_packet)
        if seq_num is not None:
            if data == b"EOF":
                print("Received immediate EOF")
                with open(output_filename, 'wb') as f:
                    f.write(b'')
                return True
            
            self.received_data[seq_num] = data
            self.max_seq_received = seq_num
            self.total_packets += 1
            
            # Check if we can advance expected_seq
            while self.expected_seq in self.received_data:
                self.expected_seq += 1
            
            # Send ACK
            self.send_ack(self.expected_seq)
        
        start_time = time.time()
        last_progress_time = start_time
        last_ack_time = time.time()
        ack_interval = 0.01  # Send ACK every 10ms if packets arriving
        
        # Main reception loop
        while True:
            try:
                packet, _ = self.socket.recvfrom(MAX_PACKET_SIZE + 100)
                seq_num, data = self.parse_packet(packet)
                
                if seq_num is None:
                    continue
                
                # Check for EOF
                if data == b"EOF":
                    print("Received EOF signal")
                    break
                
                self.total_packets += 1
                
                # Handle packet
                if seq_num < self.expected_seq:
                    # Duplicate packet
                    self.duplicate_packets += 1
                    # Send ACK anyway to help server
                    if time.time() - last_ack_time > ack_interval:
                        self.send_ack(self.expected_seq)
                        last_ack_time = time.time()
                
                elif seq_num == self.expected_seq:
                    # Expected packet
                    self.received_data[seq_num] = data
                    self.max_seq_received = max(self.max_seq_received, seq_num)
                    
                    # Advance expected_seq as far as possible
                    while self.expected_seq in self.received_data:
                        self.expected_seq += 1
                    
                    # Send ACK
                    self.send_ack(self.expected_seq)
                    last_ack_time = time.time()
                
                else:
                    # Out of order packet
                    if seq_num not in self.received_data:
                        self.out_of_order_packets += 1
                        self.received_data[seq_num] = data
                        self.max_seq_received = max(self.max_seq_received, seq_num)
                    
                    # Send ACK for what we have
                    if time.time() - last_ack_time > ack_interval:
                        self.send_ack(self.expected_seq)
                        last_ack_time = time.time()
                
                # Progress update
                now = time.time()
                if now - last_progress_time > 1.0:
                    throughput = (self.expected_seq * MSS * 8) / (now - start_time) / 1000000
                    print(f"Progress: {self.expected_seq} packets received, "
                          f"{throughput:.2f} Mbps, "
                          f"duplicates: {self.duplicate_packets}, "
                          f"out-of-order: {self.out_of_order_packets}")
                    last_progress_time = now
            
            except socket.timeout:
                # No packet received, send ACK in case it was lost
                if time.time() - last_ack_time > 0.05:  # Send ACK every 50ms during silence
                    self.send_ack(self.expected_seq)
                    last_ack_time = time.time()
                
                # Check if we've been waiting too long
                if time.time() - last_progress_time > 5.0:
                    print("No packets received for 5 seconds, assuming transfer complete")
                    break
        
        # Write received data to file
        print(f"Writing {len(self.received_data)} packets to file...")
        
        with open(output_filename, 'wb') as f:
            for seq in sorted(self.received_data.keys()):
                f.write(self.received_data[seq])
        
        # Final statistics
        end_time = time.time()
        duration = end_time - start_time
        total_bytes = len(self.received_data) * MSS
        throughput = (total_bytes * 8) / duration / 1000000
        
        print(f"\n=== Transfer Complete ===")
        print(f"Duration: {duration:.2f} seconds")
        print(f"Total packets: {self.total_packets}")
        print(f"Unique packets: {len(self.received_data)}")
        print(f"Duplicate packets: {self.duplicate_packets}")
        print(f"Out-of-order packets: {self.out_of_order_packets}")
        print(f"Total bytes: {total_bytes}")
        print(f"Average throughput: {throughput:.2f} Mbps")
        print(f"Output file: {output_filename}")
        
        return True
    
    def run(self):
        """Main client loop"""
        output_filename = f"{self.prefix}received_data.txt"
        success = self.receive_file(output_filename)
        
        if success:
            print("Client finished successfully")
        else:
            print("Client failed to receive file")
            sys.exit(1)


def main():
    if len(sys.argv) != 4:
        print("Usage: python3 p2_client.py <SERVER_IP> <SERVER_PORT> <PREF_FILENAME>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = sys.argv[2]
    prefix = sys.argv[3]
    
    client = ReliableUDPClient(server_ip, server_port, prefix)
    client.run()


if __name__ == "__main__":
    main()
