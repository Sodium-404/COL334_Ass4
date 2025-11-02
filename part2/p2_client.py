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
        
        # Receiver state
        self.expected_seq = 0
        self.received_data = {}  # {seq_num: data} - buffer for out-of-order packets
        self.file_complete = False
        
        # Statistics for congestion control analysis
        self.start_time = None
        self.end_time = None
        self.total_bytes = 0
        self.packets_received = 0
        self.duplicate_packets = 0  # Retransmissions received
        self.out_of_order_packets = 0
        self.ack_sent_count = 0
        
        self.output_file = None
        
    def make_ack_packet(self, ack_num):
        """Create ACK packet with acknowledgment number (next expected seq)"""
        # ACK number (4 bytes) + Reserved (16 bytes)
        return struct.pack('!I', ack_num) + b'\x00' * 16
    
    def make_eof_ack_packet(self):
        """Create special EOF ACK packet"""
        return struct.pack('!I', 0xFFFFFFFE) + b'\x00' * 16
    
    def parse_packet(self, packet):
        """Parse received packet to extract sequence number and data"""
        if len(packet) < 20:
            return None, None
        
        seq_num = struct.unpack('!I', packet[:4])[0]
        data = packet[20:]  # Skip 20-byte header
        
        return seq_num, data
    
    def send_request(self):
        """Send file request to server with retry logic"""
        request = b'G'  # Single byte request
        
        for attempt in range(5):
            try:
                print(f"Sending request (attempt {attempt + 1}/5)")
                self.sock.sendto(request, (self.server_ip, self.server_port))
                
                # Wait for first data packet
                self.sock.settimeout(2.0)
                data, addr = self.sock.recvfrom(1200)
                
                # Successfully received response
                print("Request successful, starting file transfer")
                self.start_time = time.time()
                return data
                
            except socket.timeout:
                print("Request timeout, retrying...")
                continue
        
        print("Failed to connect to server after 5 attempts")
        return None
    
    def send_ack(self, ack_num):
        """Send cumulative ACK to server"""
        ack_packet = self.make_ack_packet(ack_num)
        self.sock.sendto(ack_packet, (self.server_ip, self.server_port))
        self.ack_sent_count += 1
    
    def send_eof_ack(self):
        """Send EOF acknowledgment to server"""
        eof_ack_packet = self.make_eof_ack_packet()
        self.sock.sendto(eof_ack_packet, (self.server_ip, self.server_port))
    
    def process_packet(self, packet):
        """Process received packet and send appropriate ACK"""
        seq_num, data = self.parse_packet(packet)
        
        if seq_num is None:
            return
        
        # Check for EOF
        if seq_num == 0xFFFFFFFF and data == b'EOF':
            if not self.file_complete:
                print("Received EOF signal")
                self.file_complete = True
                self.end_time = time.time()
            # Always send EOF ACK
            self.send_eof_ack()
            return
        
        # If packet is the expected one, write it and all subsequent buffered packets
        if seq_num == self.expected_seq:
            # Write this packet
            if self.output_file:
                self.output_file.write(data)
                self.total_bytes += len(data)
            self.expected_seq += 1
            
            # Write any buffered packets that are now in order
            while self.expected_seq in self.received_data:
                if self.output_file:
                    buffered_data = self.received_data[self.expected_seq]
                    self.output_file.write(buffered_data)
                    self.total_bytes += len(buffered_data)
                del self.received_data[self.expected_seq]
                self.expected_seq += 1
        
        # If packet is out of order (future packet), buffer it
        elif seq_num > self.expected_seq:
            # This indicates packet loss or reordering - important for CC analysis
            if seq_num not in self.received_data:
                self.received_data[seq_num] = data
                self.out_of_order_packets += 1
        
        # If packet is old (seq < expected), it's a retransmission
        else:
            self.duplicate_packets += 1
        
        # Always send cumulative ACK with next expected sequence number
        # This is CRITICAL for congestion control - generates duplicate ACKs!
        self.send_ack(self.expected_seq)
    
    def receive_file(self):
        """Main receive loop"""
        # Open output file
        self.output_file = open('received_data.txt', 'wb')
        
        # Send initial request and get first packet
        first_packet = self.send_request()
        if first_packet is None:
            return False
        
        # Process first packet
        self.process_packet(first_packet)
        
        # Set timeout for subsequent packets
        self.sock.settimeout(5.0)
        
        # Receive remaining packets
        last_activity = time.time()
        eof_count = 0
        
        while not self.file_complete or eof_count < 3:
            try:
                data, addr = self.sock.recvfrom(1200)
                self.packets_received += 1
                last_activity = time.time()
                
                # Check if this is an EOF packet
                seq_num, packet_data = self.parse_packet(data)
                if seq_num == 0xFFFFFFFF and packet_data == b'EOF':
                    eof_count += 1
                
                self.process_packet(data)
                
                # If we've received EOF and sent ACK multiple times, we're done
                if self.file_complete and eof_count >= 3:
                    break
                
                # Print progress every 100 packets
                if self.packets_received % 100 == 0:
                    print(f"Received {self.packets_received} packets, expecting seq {self.expected_seq}")
                
            except socket.timeout:
                # If file is complete and we've acknowledged EOF, we can exit
                if self.file_complete:
                    break
                
                # Check if we've been idle too long
                if time.time() - last_activity > 10.0:
                    print("Transfer timeout - no packets received for 10 seconds")
                    break
                continue
            except Exception as e:
                print(f"Error receiving packet: {e}")
                break
        
        # Close file
        if self.output_file:
            self.output_file.close()
        
        # Print comprehensive statistics
        if self.file_complete and self.start_time and self.end_time:
            self.print_statistics()
            return True
        else:
            print("File transfer incomplete")
            return False
    
    def print_statistics(self):
        """Print detailed statistics for congestion control analysis"""
        transfer_time = self.end_time - self.start_time
        throughput_mbps = (self.total_bytes * 8) / transfer_time / 1e6
        
        print("\n" + "="*60)
        print("File Transfer Statistics")
        print("="*60)
        print(f"Total packets received: {self.packets_received}")
        print(f"Unique data packets: {self.expected_seq}")
        print(f"Out-of-order packets: {self.out_of_order_packets}")
        print(f"Duplicate packets (retransmissions): {self.duplicate_packets}")
        print(f"ACKs sent: {self.ack_sent_count}")
        print(f"Total bytes: {self.total_bytes:,}")
        print(f"Transfer time: {transfer_time:.3f} seconds")
        print(f"Throughput: {throughput_mbps:.2f} Mbps")
        
        # Calculate packet loss rate
        if self.expected_seq > 0:
            loss_rate = (self.duplicate_packets / self.packets_received) * 100
            print(f"Estimated packet loss rate: {loss_rate:.2f}%")
        
        # Calculate goodput (useful data)
        goodput_mbps = (self.expected_seq * 1180 * 8) / transfer_time / 1e6
        print(f"Goodput (excluding retransmissions): {goodput_mbps:.2f} Mbps")
        
        print("="*60)
        print(f"Output file: received_data.txt")
        print("="*60 + "\n")
    
    def run(self):
        """Main client execution"""
        print(f"Connecting to server at {self.server_ip}:{self.server_port}")
        
        try:
            success = self.receive_file()
            
            if success:
                print("Download successful!")
            else:
                print("Download failed!")
                
        except KeyboardInterrupt:
            print("\nClient interrupted")
        finally:
            self.sock.close()
            if self.output_file and not self.output_file.closed:
                self.output_file.close()
            print("Client closed")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 p1_client.py <SERVER_IP> <SERVER_PORT>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    
    client = ReliableUDPClient(server_ip, server_port)
    client.run()