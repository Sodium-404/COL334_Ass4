import socket
import sys
import time
import struct
import threading

class ReliableUDPServer:
    def __init__(self, server_ip, server_port, sws):
        self.server_ip = server_ip
        self.server_port = server_port
        self.sws = sws  # Sender window size in bytes
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.server_ip, self.server_port))
        
        # Reliability parameters
        self.seq_num = 0
        self.base = 0  # Oldest unacked sequence number
        self.next_seq = 0  # Next sequence number to send
        self.all_packets = {}  # {seq_num: (packet_data, timestamp)}
        self.duplicate_acks = {}  # Track duplicate ACKs for fast retransmit
        
        # Timeout parameters
        self.estimated_rtt = 0.5
        self.dev_rtt = 0.25
        self.timeout_interval = 1.0
        self.alpha = 0.125
        self.beta = 0.25
        
        self.lock = threading.Lock()
        self.running = True
        self.eof_acked = False  # Flag for EOF acknowledgment
        self.client_addr = None
        
    def make_packet(self, seq_num, data):
        """Create a packet with header and data"""
        # Sequence number (4 bytes) + Reserved (16 bytes) + Data
        header = struct.pack('!I', seq_num) + b'\x00' * 16
        return header + data
    
    def parse_ack(self, packet):
        """Parse ACK packet to get acknowledgment number"""
        if len(packet) < 4:
            return None
        ack_num = struct.unpack('!I', packet[:4])[0]
        return ack_num
    
    def update_rtt(self, sample_rtt):
        """Update RTO based on measured RTT"""
        with self.lock:
            if self.estimated_rtt == 0.5:  # First measurement
                self.estimated_rtt = sample_rtt
                self.dev_rtt = sample_rtt / 2
            else:
                self.estimated_rtt = (1 - self.alpha) * self.estimated_rtt + self.alpha * sample_rtt
                self.dev_rtt = (1 - self.beta) * self.dev_rtt + self.beta * abs(sample_rtt - self.estimated_rtt)
            
            self.timeout_interval = self.estimated_rtt + 4 * self.dev_rtt
            self.timeout_interval = max(0.5, min(self.timeout_interval, 3.0))
    
    def send_data(self, data_bytes):
        """Send data using sliding window protocol"""
        chunk_size = 1180  # Max data per packet
        chunks = [data_bytes[i:i+chunk_size] for i in range(0, len(data_bytes), chunk_size)]
        total_chunks = len(chunks)
        
        print(f"Sending {len(data_bytes)} bytes in {total_chunks} packets")
        
        # Prepare all packets
        for i, chunk in enumerate(chunks):
            seq = i
            packet = self.make_packet(seq, chunk)
            self.all_packets[seq] = (packet, None)
        
        # Start ACK receiver thread
        ack_thread = threading.Thread(target=self.receive_acks)
        ack_thread.daemon = True
        ack_thread.start()
        
        # Sending loop
        while self.base < total_chunks:
            with self.lock:
                # Send packets within window
                while self.next_seq < total_chunks and (self.next_seq - self.base) * chunk_size < self.sws:
                    packet, _ = self.all_packets[self.next_seq]
                    self.sock.sendto(packet, self.client_addr)
                    self.all_packets[self.next_seq] = (packet, time.time())
                    self.next_seq += 1
                
                # Check for timeouts
                if self.base < total_chunks:
                    current_time = time.time()
                    packet, send_time = self.all_packets[self.base]
                    if send_time and current_time - send_time > self.timeout_interval:
                        print(f"Timeout: Retransmitting packet {self.base}")
                        self.sock.sendto(packet, self.client_addr)
                        self.all_packets[self.base] = (packet, current_time)
            
            time.sleep(0.001)  # Small delay to prevent busy waiting
        
        print("All packets acknowledged")
    
    def receive_acks(self):
        """Receive and process ACKs"""
        self.sock.settimeout(0.1)
        last_ack = -1
        
        while self.running:
            try:
                data, addr = self.sock.recvfrom(1200)
                ack_num = self.parse_ack(data)
                
                if ack_num is None:
                    continue
                
                # Check for EOF ACK (special value 0xFFFFFFFE)
                if ack_num == 0xFFFFFFFE:
                    with self.lock:
                        self.eof_acked = True
                    print("Received EOF ACK from client")
                    continue
                
                with self.lock:
                    # Cumulative ACK: ack_num is next expected sequence number
                    if ack_num > self.base:
                        # Calculate RTT for packets being acknowledged
                        for seq in range(self.base, ack_num):
                            if seq in self.all_packets and self.all_packets[seq][1]:
                                sample_rtt = time.time() - self.all_packets[seq][1]
                                self.update_rtt(sample_rtt)
                                break
                        
                        self.base = ack_num
                        self.duplicate_acks.clear()
                        last_ack = ack_num
                    
                    elif ack_num == last_ack:
                        # Duplicate ACK
                        self.duplicate_acks[ack_num] = self.duplicate_acks.get(ack_num, 0) + 1
                        
                        # Fast retransmit after 3 duplicate ACKs
                        if self.duplicate_acks[ack_num] == 3:
                            if ack_num < len(self.all_packets):
                                print(f"Fast retransmit: packet {ack_num}")
                                packet, _ = self.all_packets[ack_num]
                                self.sock.sendto(packet, self.client_addr)
                                self.all_packets[ack_num] = (packet, time.time())
            
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"Error receiving ACK: {e}")
                break
    
    def send_eof(self):
        """Send EOF signal and wait for EOF ACK"""
        eof_packet = self.make_packet(0xFFFFFFFF, b'EOF')
        max_attempts = 10  # Maximum attempts
        attempt = 0
        
        print("Sending EOF and waiting for acknowledgment...")
        
        while not self.eof_acked and attempt < max_attempts:
            self.sock.sendto(eof_packet, self.client_addr)
            time.sleep(0.2)  # Wait 200ms between EOF sends
            attempt += 1
            
            if attempt % 5 == 0 and not self.eof_acked:
                print(f"Still waiting for EOF ACK (attempt {attempt}/{max_attempts})...")
        
        if self.eof_acked:
            print("EOF acknowledged by client")
            return True
        else:
            print("Warning: EOF not acknowledged after maximum attempts")
            return False
    
    def run(self):
        """Main server loop"""
        print(f"Server listening on {self.server_ip}:{self.server_port}")
        print(f"Sender window size: {self.sws} bytes")
        
        # Wait for client request
        try:
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
            
            print(f"Transfer completed in {end_time - start_time:.2f} seconds")
            
            # Send EOF and wait for acknowledgment
            eof_success = self.send_eof()
            
            if eof_success:
                print("Connection closed gracefully")
            else:
                print("Connection closed with timeout")
            
        except KeyboardInterrupt:
            print("\nServer interrupted")
        finally:
            self.running = False
            self.sock.close()
            print("Server closed")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python3 p1_server.py <SERVER_IP> <SERVER_PORT> <SWS>")
        sys.exit(1)
    
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    sws = int(sys.argv[3])
    
    server = ReliableUDPServer(server_ip, server_port, sws)
    server.run()