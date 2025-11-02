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
        
        self.output_file = None
        
    def make_ack_packet(self, ack_num):
        """Create ACK packet with acknowledgment number (next expected seq)"""
        # ACK number (4 bytes) + Reserved (16 bytes)
        return struct.pack('!I', ack_num) + b'\x00' * 16
    
    def make_eof_ack_packet(self):
        """Create special EOF ACK packet"""
        # Use special value 0xFFFFFFFE to indicate EOF acknowledgment
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
    
    def send_eof_ack(self):
        """Send EOF acknowledgment to server"""
        eof_ack_packet = self.make_eof_ack_packet()
        self.sock.sendto(eof_ack_packet, (self.server_ip, self.server_port))
        print("Sent EOF ACK to server")
    
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
            # Always send EOF ACK when we receive EOF (even if duplicate)
            self.send_eof_ack()
            return
        
        # If packet is the expected one, write it and all subsequent buffered packets
        if seq_num == self.expected_seq:
            # Write this packet
            if self.output_file:
                self.output_file.write(data)
            self.expected_seq += 1
            
            # Write any buffered packets that are now in order
            while self.expected_seq in self.received_data:
                if self.output_file:
                    self.output_file.write(self.received_data[self.expected_seq])
                del self.received_data[self.expected_seq]
                self.expected_seq += 1
        
        # If packet is out of order (future packet), buffer it
        elif seq_num > self.expected_seq:
            if seq_num not in self.received_data:
                self.received_data[seq_num] = data
        
        # If packet is old (seq < expected), ignore it (already received)
        
        # Always send cumulative ACK with next expected sequence number
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
        packets_received = 1
        eof_count = 0  # Count EOF packets to handle multiple EOFs
        
        while not self.file_complete or eof_count < 3:
            try:
                data, addr = self.sock.recvfrom(1200)
                packets_received += 1
                last_activity = time.time()
                
                # Check if this is an EOF packet
                seq_num, packet_data = self.parse_packet(data)
                if seq_num == 0xFFFFFFFF and packet_data == b'EOF':
                    eof_count += 1
                
                self.process_packet(data)
                
                # If we've received EOF and sent ACK multiple times, we're done
                if self.file_complete and eof_count >= 3:
                    print("EOF handshake complete")
                    break
                
                # Print progress every 100 packets
                if packets_received % 100 == 0:
                    print(f"Received {packets_received} packets, expecting seq {self.expected_seq}")
                
            except socket.timeout:
                # If file is complete and we've acknowledged EOF, we can exit
                if self.file_complete:
                    print("File transfer complete, EOF acknowledged")
                    break
                
                # Check if we've been idle too long
                if time.time() - last_activity > 10.0:
                    print("Transfer timeout - no packets received for 10 seconds")
                    break
                # Otherwise just continue waiting
                continue
            except Exception as e:
                print(f"Error receiving packet: {e}")
                break
        
        # Close file
        if self.output_file:
            self.output_file.close()
        
        if self.file_complete:
            print(f"File transfer complete! Received {packets_received} packets")
            print(f"Wrote data to received_data.txt")
            return True
        else:
            print("File transfer incomplete")
            return False
    
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