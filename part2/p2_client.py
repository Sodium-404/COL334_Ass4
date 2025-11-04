#!/usr/bin/env python3
"""
Part 2 Client: Reliable UDP file receiver with ACK and SACK support
"""

import socket
import sys
import struct
import time

# Constants
MSS = 1180
HEADER_SIZE = 20
MAX_PACKET_SIZE = 1200
INITIAL_TIMEOUT = 2.0
MAX_RETRIES = 5


class ReliableUDPClient:
    def __init__(self, server_ip, server_port, prefix):
        self.server_ip = server_ip
        self.server_port = int(server_port)
        self.prefix = prefix
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(INITIAL_TIMEOUT)
        
        self.expected_seq = 0
        self.received_data = {}  # seq_num -> data
        self.max_received_seq = -1
        
        print(f"[Client] Connecting to {server_ip}:{server_port}")
    
    def parse_packet(self, packet):
        """Parse packet to extract sequence number and data"""
        if len(packet) < HEADER_SIZE:
            return None, None
        
        seq_num = struct.unpack('!I', packet[:4])[0]
        data = packet[HEADER_SIZE:]
        
        return seq_num, data
    
    def make_ack(self, ack_num):
        """Create ACK packet with cumulative ACK and SACK blocks"""
        # Build SACK blocks for out-of-order packets
        sack_blocks = []
        
        if self.received_data:
            # Find gaps in received sequence numbers
            sorted_seqs = sorted(self.received_data.keys())
            
            if sorted_seqs:
                # Look for contiguous blocks after expected_seq
                current_start = None
                current_end = None
                
                for seq in sorted_seqs:
                    if seq <= self.expected_seq:
                        continue
                    
                    if current_start is None:
                        current_start = seq
                        current_end = seq + 1
                    elif seq == current_end:
                        current_end = seq + 1
                    else:
                        # Gap found, save current block
                        if len(sack_blocks) < 2:
                            sack_blocks.append((current_start, current_end))
                        current_start = seq
                        current_end = seq + 1
                
                # Save last block
                if current_start is not None and len(sack_blocks) < 2:
                    sack_blocks.append((current_start, current_end))
        
        # Create ACK packet: ack_num (4 bytes) + SACK blocks (16 bytes)
        ack_packet = struct.pack('!I', ack_num)
        
        # Add SACK blocks (up to 2 blocks, 8 bytes each)
        if len(sack_blocks) >= 1:
            ack_packet += struct.pack('!II', sack_blocks[0][0], sack_blocks[0][1])
        else:
            ack_packet += struct.pack('!II', 0, 0)
        
        if len(sack_blocks) >= 2:
            ack_packet += struct.pack('!II', sack_blocks[1][0], sack_blocks[1][1])
        else:
            ack_packet += struct.pack('!II', 0, 0)
        
        return ack_packet
    
    def send_ack(self, ack_num):
        """Send ACK to server"""
        ack_packet = self.make_ack(ack_num)
        self.sock.sendto(ack_packet, (self.server_ip, self.server_port))
    
    def send_request(self):
        """Send file request to server"""
        request = b'\x01'  # Simple one-byte request
        
        for attempt in range(MAX_RETRIES):
            try:
                print(f"[Client] Sending request (attempt {attempt + 1}/{MAX_RETRIES})")
                self.sock.sendto(request, (self.server_ip, self.server_port))
                
                # Wait for first packet
                self.sock.settimeout(2.0)
                packet, _ = self.sock.recvfrom(MAX_PACKET_SIZE)
                
                # Put the first packet back for processing
                seq_num, data = self.parse_packet(packet)
                if seq_num is not None:
                    print(f"[Client] Connection established")
                    # Process this first packet
                    return packet
                
            except socket.timeout:
                print(f"[Client] Request timeout")
                continue
        
        print("[Client] Failed to connect after max retries")
        return None
    
    def receive_file(self, output_filename):
        """Receive file from server"""
        # Send request and get first packet
        first_packet = self.send_request()
        if first_packet is None:
            print("[Client] Failed to establish connection")
            return False
        
        print(f"[Client] Receiving file, saving to {output_filename}")
        
        # Process first packet
        seq_num, data = self.parse_packet(first_packet)
        if seq_num == 0 and data != b"EOF":
            self.received_data[seq_num] = data
            self.expected_seq = 1
            self.max_received_seq = 0
            self.send_ack(self.expected_seq)
        
        last_ack_time = time.time()
        ack_interval = 0.01  # Send ACK every 10ms or when needed
        consecutive_timeouts = 0
        max_consecutive_timeouts = 10
        
        start_time = time.time()
        last_print_time = start_time
        last_received_time = time.time()
        
        self.sock.settimeout(1.0)
        
        while True:
            try:
                # Receive packet
                packet, _ = self.sock.recvfrom(MAX_PACKET_SIZE)
                last_received_time = time.time()
                consecutive_timeouts = 0
                
                seq_num, data = self.parse_packet(packet)
                
                if seq_num is None:
                    continue
                
                # Check for EOF
                if data == b"EOF":
                    print("[Client] Received EOF marker")
                    self.send_ack(self.expected_seq)
                    break
                
                # Store packet if not duplicate
                if seq_num not in self.received_data:
                    self.received_data[seq_num] = data
                    self.max_received_seq = max(self.max_received_seq, seq_num)
                
                # Update expected sequence number (cumulative ACK)
                while self.expected_seq in self.received_data:
                    self.expected_seq += 1
                
                # Send ACK
                current_time = time.time()
                if current_time - last_ack_time >= ack_interval or seq_num != self.expected_seq - 1:
                    self.send_ack(self.expected_seq)
                    last_ack_time = current_time
                
                # Progress report
                if current_time - last_print_time > 2.0:
                    elapsed = current_time - start_time
                    received_bytes = len(self.received_data) * MSS
                    throughput = (received_bytes * 8) / (elapsed * 1e6)
                    print(f"[Client] Received {len(self.received_data)} packets, expected_seq={self.expected_seq}, throughput={throughput:.2f} Mbps")
                    last_print_time = current_time
                
            except socket.timeout:
                consecutive_timeouts += 1
                current_time = time.time()
                
                # Send duplicate ACK to trigger fast retransmit
                self.send_ack(self.expected_seq)
                
                # Check if we've been idle too long
                if current_time - last_received_time > 5.0:
                    print("[Client] No data received for 5 seconds")
                    if self.received_data:
                        print("[Client] Assuming transfer complete")
                        break
                    else:
                        print("[Client] Connection lost")
                        return False
                
                if consecutive_timeouts >= max_consecutive_timeouts:
                    print("[Client] Too many consecutive timeouts")
                    if self.received_data:
                        print("[Client] Assuming transfer complete")
                        break
                    else:
                        return False
        
        # Write received data to file
        try:
            with open(output_filename, 'wb') as f:
                for seq in sorted(self.received_data.keys()):
                    f.write(self.received_data[seq])
            
            file_size = len(self.received_data) * MSS
            elapsed = time.time() - start_time
            throughput = (file_size * 8) / (elapsed * 1e6)
            
            print(f"[Client] File received successfully")
            print(f"[Client] Total packets: {len(self.received_data)}, Size: {file_size} bytes")
            print(f"[Client] Time: {elapsed:.2f}s, Throughput: {throughput:.2f} Mbps")
            return True
            
        except Exception as e:
            print(f"[Client] Error writing file: {e}")
            return False
    
    def run(self):
        """Main client loop"""
        output_filename = f"{self.prefix}received_data.txt"
        success = self.receive_file(output_filename)
        self.sock.close()
        
        if success:
            print("[Client] Transfer complete")
        else:
            print("[Client] Transfer failed")
        
        return success


def main():
    if len(sys.argv) != 4:
        print("Usage: python3 p2_client.py <SERVER_IP> <SERVER_PORT> <PREF_FILENAME>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = sys.argv[2]
    prefix = sys.argv[3]
    
    client = ReliableUDPClient(server_ip, server_port, prefix)
    success = client.run()
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()