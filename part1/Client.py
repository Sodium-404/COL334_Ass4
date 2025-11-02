#!/usr/bin/env python3

import socket
import sys
import time
import struct

class ReliableUDPClient:
    def __init__(self, server_ip, server_port):
        self.server_ip = server_ip
        self.server_port = server_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)
        
        # Receiver state - using packet numbers (not byte offsets)
        self.received_packets = set()  # Set of received packet sequence numbers
        self.file_complete = False
        
    def make_sack_packet(self):
        """Create SACK packet with selective acknowledgments.
        
        SACK packet format:
        - Byte 0-3: Next expected packet number (first missing)
        - Bytes 4-19: Up to 4 SACK ranges (each range is 2 bytes start + 2 bytes length)
        """
        if not self.received_packets:
            # No packets received yet
            return struct.pack('!I', 0) + b'\x00' * 16
        
        sorted_packets = sorted(self.received_packets)
        
        # Find next expected (first missing packet)
        next_expected = 0
        for pkt in sorted_packets:
            if pkt == next_expected:
                next_expected += 1
            else:
                break
        
        # Build SACK ranges (contiguous blocks beyond next_expected)
        sack_ranges = []
        current_range_start = None
        current_range_len = 0
        
        for pkt in sorted_packets:
            if pkt < next_expected:
                continue  # Skip packets before next_expected
            
            if current_range_start is None:
                current_range_start = pkt
                current_range_len = 1
            elif pkt == current_range_start + current_range_len:
                current_range_len += 1
            else:
                # Gap found, save current range
                sack_ranges.append((current_range_start, current_range_len))
                current_range_start = pkt
                current_range_len = 1
                
                if len(sack_ranges) >= 4:
                    break
        
        # Add final range
        if current_range_start is not None and len(sack_ranges) < 4:
            sack_ranges.append((current_range_start, current_range_len))
        
        # Pack into packet
        sack_data = struct.pack('!I', next_expected)
        
        for start, length in sack_ranges[:4]:
            sack_data += struct.pack('!HH', start & 0xFFFF, length & 0xFFFF)
        
        # Pad remaining space
        remaining = 16 - (len(sack_ranges) * 4)
        sack_data += b'\x00' * remaining
        
        return sack_data
    
    def make_eof_ack_packet(self):
        """Create special EOF ACK packet"""
        return struct.pack('!I', 0xFFFFFFFE) + b'\x00' * 16
    
    def parse_packet(self, packet):
        """Parse received packet"""
        if len(packet) < 20:
            return None, None
        
        seq_num = struct.unpack('!I', packet[:4])[0]
        data = packet[20:]
        
        return seq_num, data
    
    def send_request(self):
        """Send file request to server"""
        request = b'G'
        
        for attempt in range(5):
            try:
                print(f"Sending request (attempt {attempt + 1}/5)")
                self.sock.sendto(request, (self.server_ip, self.server_port))
                
                self.sock.settimeout(2.0)
                data, addr = self.sock.recvfrom(1200)
                
                print("Request successful, starting file transfer")
                return data
                
            except socket.timeout:
                print("Request timeout, retrying...")
                continue
        
        print("Failed to connect after 5 attempts")
        return None
    
    def send_sack(self):
        """Send SACK to server"""
        sack_packet = self.make_sack_packet()
        self.sock.sendto(sack_packet, (self.server_ip, self.server_port))
    
    def send_eof_ack(self):
        """Send EOF ACK"""
        eof_ack = self.make_eof_ack_packet()
        self.sock.sendto(eof_ack, (self.server_ip, self.server_port))
    
    def process_packet(self, packet, packet_data_map):
        """Process packet and send SACK"""
        seq_num, data = self.parse_packet(packet)
        
        if seq_num is None:
            return
        
        # Check for EOF
        if seq_num == 0xFFFFFFFF and data == b'EOF':
            if not self.file_complete:
                print("Received EOF signal")
                self.file_complete = True
            self.send_eof_ack()
            return
        
        # Store packet if new
        if seq_num not in self.received_packets:
            self.received_packets.add(seq_num)
            packet_data_map[seq_num] = data
        
        # Always send SACK
        self.send_sack()
    
    def write_file(self, packet_data_map):
        """Write received data to file"""
        if not self.received_packets:
            return 0
        
        max_seq = max(self.received_packets)
        bytes_written = 0
        
        with open('received_data.txt', 'wb') as f:
            for seq in range(max_seq + 1):
                if seq in packet_data_map:
                    f.write(packet_data_map[seq])
                    bytes_written += len(packet_data_map[seq])
                else:
                    print(f"Warning: Missing packet {seq}")
        
        return bytes_written
    
    def receive_file(self):
        """Main receive loop"""
        packet_data_map = {}
        
        # Get first packet
        first_packet = self.send_request()
        if first_packet is None:
            return False
        
        self.process_packet(first_packet, packet_data_map)
        
        self.sock.settimeout(3.0)
        
        packets_received = 1
        eof_count = 0
        consecutive_timeouts = 0
        last_activity = time.time()
        
        while not self.file_complete or eof_count < 3:
            try:
                data, addr = self.sock.recvfrom(1200)
                packets_received += 1
                last_activity = time.time()
                consecutive_timeouts = 0
                
                seq_num, packet_data = self.parse_packet(data)
                if seq_num == 0xFFFFFFFF and packet_data == b'EOF':
                    eof_count += 1
                
                self.process_packet(data, packet_data_map)
                
                if self.file_complete and eof_count >= 3:
                    break
                
                if packets_received % 200 == 0:
                    print(f"Received {packets_received} packets, {len(self.received_packets)} unique")
                
            except socket.timeout:
                consecutive_timeouts += 1
                
                if not self.file_complete:
                    self.send_sack()
                
                if self.file_complete:
                    print("File complete, EOF acknowledged")
                    break
                
                if time.time() - last_activity > 15.0:
                    print("Transfer timeout")
                    break
                
                if consecutive_timeouts > 10:
                    print("Too many timeouts")
                    break
                    
                continue
                
            except Exception as e:
                print(f"Error: {e}")
                break
        
        if self.file_complete:
            bytes_written = self.write_file(packet_data_map)
            print(f"Transfer complete! {packets_received} packets, {bytes_written} bytes")
            return True
        else:
            print("Transfer incomplete")
            return False
    
    def run(self):
        """Main client"""
        print(f"Connecting to {self.server_ip}:{self.server_port}")
        print("Using SACK only")
        
        try:
            success = self.receive_file()
            print("Download successful!" if success else "Download failed!")
        except KeyboardInterrupt:
            print("\nInterrupted")
        finally:
            self.sock.close()

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 p1_client.py <SERVER_IP> <SERVER_PORT>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    
    client = ReliableUDPClient(server_ip, server_port)
    client.run()