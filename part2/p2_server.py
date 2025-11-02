import socket
import sys
import time
import struct
import threading
import math

class ReliableUDPServer:
    def __init__(self, server_ip, server_port, sws):
        self.server_ip = server_ip
        self.server_port = server_port
        self.max_sws = sws  # Maximum sender window size in bytes
        self.mss = 1180  # Maximum segment size (data per packet)
        
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.server_ip, self.server_port))
        
        # Reliability parameters
        self.base = 0  # Oldest unacked sequence number
        self.next_seq = 0  # Next sequence number to send
        self.all_packets = {}  # {seq_num: (packet_data, timestamp)}
        
        # Congestion control parameters (TCP CUBIC)
        self.cwnd = 1.0  # Congestion window (in MSS units) - start with 1 MSS
        self.ssthresh = 64.0  # Slow start threshold (in MSS units)
        self.w_max = 0.0  # Window size before last congestion event
        self.epoch_start = 0.0  # Time when current epoch started
        self.k = 0.0  # Time period to reach w_max
        self.ack_count = 0  # Count ACKs in congestion avoidance
        
        # CUBIC constants
        self.C = 0.4  # Scaling constant
        self.beta = 0.7  # Multiplicative decrease factor (0.7 for CUBIC)
        
        # Fast retransmit/recovery
        self.duplicate_acks = {}  # Track duplicate ACKs
        self.in_fast_recovery = False
        self.recovery_point = 0  # Seq number to exit fast recovery
        
        # Timeout parameters
        self.estimated_rtt = 0.5
        self.dev_rtt = 0.25
        self.timeout_interval = 1.0
        self.alpha = 0.125
        self.beta_rtt = 0.25
        
        self.lock = threading.Lock()
        self.running = True
        self.eof_acked = False
        self.client_addr = None
        
    def make_packet(self, seq_num, data):
        """Create a packet with header and data"""
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
        if self.estimated_rtt == 0.5:  # First measurement
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.estimated_rtt = (1 - self.alpha) * self.estimated_rtt + self.alpha * sample_rtt
            self.dev_rtt = (1 - self.beta_rtt) * self.dev_rtt + self.beta_rtt * abs(sample_rtt - self.estimated_rtt)
        
        self.timeout_interval = self.estimated_rtt + 4 * self.dev_rtt
        self.timeout_interval = max(0.5, min(self.timeout_interval, 3.0))
    
    def get_cwnd_bytes(self):
        """Get congestion window in bytes"""
        return min(int(self.cwnd * self.mss), self.max_sws)
    
    def cubic_update(self):
        """CUBIC window update during congestion avoidance"""
        if self.epoch_start == 0:
            self.epoch_start = time.time()
            if self.cwnd < self.w_max:
                self.k = math.pow((self.w_max - self.cwnd) / self.C, 1/3.0)
            else:
                self.k = 0
        
        t = time.time() - self.epoch_start
        target = self.C * math.pow(t - self.k, 3) + self.w_max
        
        if target > self.cwnd:
            # CUBIC increase
            cnt = self.cwnd / (target - self.cwnd)
        else:
            # TCP-friendly region
            cnt = 100 * self.cwnd
        
        return cnt
    
    def on_ack_received(self, ack_num):
        """Handle ACK reception and update congestion window"""
        with self.lock:
            if ack_num <= self.base:
                return  # Old ACK, ignore
            
            # Calculate RTT sample for the first packet being acked
            if self.base in self.all_packets and self.all_packets[self.base][1]:
                sample_rtt = time.time() - self.all_packets[self.base][1]
                self.update_rtt(sample_rtt)
            
            # Number of newly acknowledged packets
            newly_acked = ack_num - self.base
            self.base = ack_num
            
            # Exit fast recovery if we've passed the recovery point
            if self.in_fast_recovery and ack_num >= self.recovery_point:
                self.in_fast_recovery = False
                self.cwnd = self.ssthresh
                print(f"Exiting fast recovery: cwnd={self.cwnd:.2f} MSS ({self.get_cwnd_bytes()} bytes)")
            
            # Update congestion window if not in fast recovery
            if not self.in_fast_recovery:
                if self.cwnd < self.ssthresh:
                    # Slow start: exponential growth
                    self.cwnd += newly_acked
                    print(f"Slow start: cwnd={self.cwnd:.2f} MSS ({self.get_cwnd_bytes()} bytes)")
                else:
                    # Congestion avoidance: CUBIC
                    self.ack_count += newly_acked
                    cnt = self.cubic_update()
                    
                    if self.ack_count >= cnt:
                        self.cwnd += 1
                        self.ack_count = 0
                        print(f"Congestion avoidance (CUBIC): cwnd={self.cwnd:.2f} MSS ({self.get_cwnd_bytes()} bytes)")
            
            # Clear duplicate ACK counter
            self.duplicate_acks.clear()
    
    def on_duplicate_ack(self, ack_num):
        """Handle duplicate ACK"""
        with self.lock:
            self.duplicate_acks[ack_num] = self.duplicate_acks.get(ack_num, 0) + 1
            
            # Fast retransmit on 3rd duplicate ACK
            if self.duplicate_acks[ack_num] == 3:
                print(f"Fast retransmit triggered at seq {ack_num}")
                
                # Retransmit the missing packet
                if ack_num < len(self.all_packets):
                    packet, _ = self.all_packets[ack_num]
                    self.sock.sendto(packet, self.client_addr)
                    self.all_packets[ack_num] = (packet, time.time())
                
                # Enter fast recovery (TCP Reno style)
                if not self.in_fast_recovery:
                    self.ssthresh = max(self.cwnd * self.beta, 2.0)
                    self.w_max = self.cwnd  # Save for CUBIC
                    self.cwnd = self.ssthresh + 3  # Inflate window
                    self.in_fast_recovery = True
                    self.recovery_point = self.next_seq
                    self.epoch_start = 0  # Reset CUBIC epoch
                    print(f"Entering fast recovery: ssthresh={self.ssthresh:.2f}, cwnd={self.cwnd:.2f} MSS")
            
            # Inflate window during fast recovery
            elif self.duplicate_acks[ack_num] > 3 and self.in_fast_recovery:
                self.cwnd += 1
    
    def on_timeout(self, seq_num):
        """Handle timeout event"""
        with self.lock:
            print(f"Timeout at seq {seq_num}")
            
            # Severe congestion: reduce window drastically
            self.ssthresh = max(self.cwnd / 2, 2.0)
            self.w_max = self.cwnd  # Save for CUBIC
            self.cwnd = 1.0  # Reset to 1 MSS
            self.in_fast_recovery = False
            self.duplicate_acks.clear()
            self.epoch_start = 0  # Reset CUBIC epoch
            
            print(f"Timeout recovery: ssthresh={self.ssthresh:.2f}, cwnd=1.0 MSS")
            
            # Retransmit the packet
            if seq_num in self.all_packets:
                packet, _ = self.all_packets[seq_num]
                self.sock.sendto(packet, self.client_addr)
                self.all_packets[seq_num] = (packet, time.time())
    
    def send_data(self, data_bytes):
        """Send data using sliding window with congestion control"""
        chunks = [data_bytes[i:i+self.mss] for i in range(0, len(data_bytes), self.mss)]
        total_chunks = len(chunks)
        
        print(f"Sending {len(data_bytes)} bytes in {total_chunks} packets")
        print(f"Initial cwnd: {self.cwnd} MSS ({self.get_cwnd_bytes()} bytes)")
        
        # Prepare all packets
        for i, chunk in enumerate(chunks):
            packet = self.make_packet(i, chunk)
            self.all_packets[i] = (packet, None)
        
        # Start ACK receiver thread
        ack_thread = threading.Thread(target=self.receive_acks)
        ack_thread.daemon = True
        ack_thread.start()
        
        # Sending loop
        while self.base < total_chunks:
            with self.lock:
                # Send packets within congestion window
                cwnd_bytes = self.get_cwnd_bytes()
                while (self.next_seq < total_chunks and 
                       (self.next_seq - self.base) * self.mss < cwnd_bytes):
                    packet, _ = self.all_packets[self.next_seq]
                    self.sock.sendto(packet, self.client_addr)
                    self.all_packets[self.next_seq] = (packet, time.time())
                    self.next_seq += 1
                
                # Check for timeout on base packet
                if self.base < total_chunks:
                    packet, send_time = self.all_packets[self.base]
                    if send_time and time.time() - send_time > self.timeout_interval:
                        self.on_timeout(self.base)
            
            time.sleep(0.001)
        
        print(f"All packets acknowledged. Final cwnd: {self.cwnd:.2f} MSS")
    
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
                
                # Check for EOF ACK
                if ack_num == 0xFFFFFFFE:
                    with self.lock:
                        self.eof_acked = True
                    continue
                
                # New ACK
                if ack_num > last_ack:
                    self.on_ack_received(ack_num)
                    last_ack = ack_num
                # Duplicate ACK
                elif ack_num == last_ack and ack_num > 0:
                    self.on_duplicate_ack(ack_num)
            
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"Error receiving ACK: {e}")
                break
    
    def send_eof(self):
        """Send EOF signal and wait for EOF ACK"""
        eof_packet = self.make_packet(0xFFFFFFFF, b'EOF')
        max_attempts = 50
        attempt = 0
        
        print("Sending EOF and waiting for acknowledgment...")
        
        while not self.eof_acked and attempt < max_attempts:
            self.sock.sendto(eof_packet, self.client_addr)
            time.sleep(0.2)
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
        print(f"Maximum sender window size: {self.max_sws} bytes")
        
        try:
            data, self.client_addr = self.sock.recvfrom(1200)
            print(f"Received request from {self.client_addr}")
            
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
            throughput = (len(file_data) * 8) / transfer_time / 1e6  # Mbps
            
            print(f"Transfer completed in {transfer_time:.2f} seconds")
            print(f"Throughput: {throughput:.2f} Mbps")
            
            # Send EOF
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