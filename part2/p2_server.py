#!/usr/bin/env python3
"""
Part 2 Server: Reliable UDP file transfer with CUBIC congestion control
OPTIMIZED VERSION: Single timer for base packet only
CORRECTED FOR HIGH UTILIZATION
"""
import socket
import sys
import time
import struct
import math
# Constants
MSS = 1180
HEADER_SIZE = 20
MAX_PACKET_SIZE = 1200
INITIAL_TIMEOUT = 1.0
ALPHA_RTT = 0.125
BETA_RTT = 0.25
INITIAL_CWND = 1 * MSS  # 1 packet
INITIAL_SSTHRESH = 128 * MSS  # 64 packets
CUBIC_C = 1.0 # Linux default, less aggressive
CUBIC_BETA = 0.85
FAST_CONVERGENCE = True
class CubicCongestionControl:
    """CUBIC congestion control algorithm - CORRECTED FOR PROPER SCALING"""
    def __init__(self):
        self.cwnd = INITIAL_CWND
        self.ssthresh = INITIAL_SSTHRESH
        self.w_max = 0
        self.w_last_max = 0
        self.epoch_start = -1  # FIXED: Start as -1
        self.K = 0
        self.ack_count = 0
        self.last_sample_time = time.time()
        self.in_slow_start = True
        
        # TCP-friendly window tracking
        self.tcp_cwnd = INITIAL_CWND
        self.cwnd_cnt = 0  # Counter for cwnd increments
    def on_ack(self, bytes_acked, rtt):
        """Called when ACK is received - SCALED FOR CUMULATIVE"""
        num_pkts_acked = bytes_acked // MSS
        if self.cwnd < self.ssthresh:
            # Slow start: exponential growth
            self.cwnd += bytes_acked
            self.in_slow_start = True
        else:
            # Congestion avoidance: use CUBIC
            self.in_slow_start = False
            # FIXED: Call update num_pkts_acked times (or scale ack_count)
            for _ in range(num_pkts_acked):
                self._cubic_update(rtt)
    def _cubic_update(self, rtt):
        """
        CUBIC update (units fixed).
        - All CUBIC math (w_max, K, target) is done in *packets*.
        - self.cwnd remains in *bytes* (as elsewhere in your code).
        """
        # If we haven't experienced loss yet, use TCP Reno until first loss
        if self.w_max == 0:
            # FIXED: Proper Reno in packets
            self.cwnd_cnt += 1
            if self.cwnd_cnt >= self.cwnd / MSS:
                self.cwnd += MSS
                self.cwnd_cnt = 0
            return  # Skip cubic until first loss

        # Initialize epoch if needed
        if self.epoch_start == -1:
            self.epoch_start = time.time()
            # WORK IN PACKETS for CUBIC math
            w_max_pkts = (self.w_max / MSS) if self.w_max > 0 else 1.0  # FIXED: Avoid zero
            # K computed from w_max in packets
            self.K = math.pow(max(w_max_pkts * (1 - CUBIC_BETA) / CUBIC_C, 1e-6), 1/3.0)
            self.tcp_cwnd = self.cwnd
            self.ack_count = 0
            self.cwnd_cnt = 0
            print(f"[CUBIC] New epoch: cwnd={self.cwnd/MSS:.1f} pkts, w_max={w_max_pkts:.1f} pkts, K={self.K:.3f}s")

        # time since epoch
        t = time.time() - self.epoch_start
        # do cubic math in packets
        w_max_pkts = self.w_max / MSS
        target_pkts = w_max_pkts + CUBIC_C * (t - self.K) ** 3
        # convert target back to bytes for internal cwnd (self.cwnd is bytes)
        target_bytes = target_pkts * MSS
        # FIXED: Smoother per-ACK increase towards target
        gap = max(0, target_bytes - self.cwnd)
        if gap > 0:
            # Inc per ACK: min Reno (1/cwnd), max full gap smoothed
            inc_per_ack = max(MSS / (self.cwnd / MSS), gap / max(1, self.cwnd / MSS))
            self.cwnd = int(min(target_bytes, self.cwnd + min(MSS, inc_per_ack)))
        else:
            # Conservative additive if below target (rare)
            self.cwnd_cnt += 1
            if self.cwnd_cnt >= self.cwnd / MSS:
                self.cwnd += MSS
                self.cwnd_cnt = 0
    def on_loss_detected(self, loss_type='timeout'):
        """Called when packet loss is detected"""
        print(f"[CUBIC] Loss detected ({loss_type}): cwnd={self.cwnd/MSS:.1f} -> ", end='')
        
        if loss_type == 'timeout':
            # Timeout: severe, reset to 1 MSS
            self.ssthresh = max(self.cwnd // 2, 2 * MSS)
            
            # Fast convergence
            if FAST_CONVERGENCE and self.w_max > 0 and self.cwnd < self.w_last_max:
                self.w_last_max = self.w_max
                self.w_max = self.cwnd * (1 + CUBIC_BETA) / 2
            else:
                self.w_last_max = self.w_max
                self.w_max = self.cwnd
            
            self.cwnd = 1 * MSS
            self.epoch_start = -1
            self.K = 0
            self.ack_count = 0
            self.cwnd_cnt = 0
            self.in_slow_start = True
            self.tcp_cwnd = MSS
            
        else:  # fast_retransmit
            # Multiplicative decrease by beta
            if FAST_CONVERGENCE and self.w_max > 0 and self.cwnd < self.w_last_max:
                self.w_last_max = self.w_max
                self.w_max = self.cwnd * (1 + CUBIC_BETA) / 2
            else:
                self.w_last_max = self.w_max
                self.w_max = self.cwnd
            
            self.cwnd = int(self.cwnd * CUBIC_BETA)
            self.ssthresh = max(self.cwnd, 2 * MSS)
            self.epoch_start = -1
            self.K = 0
            self.ack_count = 0
            self.cwnd_cnt = 0
            self.in_slow_start = False
            self.tcp_cwnd = self.cwnd
        
        print(f"{self.cwnd/MSS:.1f}, w_max={self.w_max/MSS:.1f}, ssthresh={self.ssthresh/MSS:.1f}, K will be {math.pow(self.w_max * (1 - CUBIC_BETA) / CUBIC_C, 1/3.0):.3f}s")
class ReliableUDPServer:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(5.0)
        self.seq_num = 0
        self.next_seq_num = 0
        self.base = 0
        self.estimated_rtt = INITIAL_TIMEOUT
        self.dev_rtt = 0
        self.rto = INITIAL_TIMEOUT
        self.cc = CubicCongestionControl()
        self.sent_packets = {}
        self.dup_ack_count = {}
        self.client_addr = None
        
        # OPTIMIZATION: Single timer for base packet
        self.timer_start = None  # Track when base packet was sent
        print(f"[Server] Started on {host}:{port}")
        print(f"[Server] Initial cwnd: {self.cc.cwnd / MSS:.1f} packets, ssthresh: {self.cc.ssthresh / MSS:.1f} packets")
    def make_packet(self, seq_num, data):
        header = struct.pack('!I', seq_num) + b'\x00' * 16
        return header + data
    def parse_ack(self, packet):
        if len(packet) < 4:
            return None, []
        ack_num = struct.unpack('!I', packet[:4])[0]
        sack_blocks = []
        if len(packet) >= 20:
            try:
                sack1_start = struct.unpack('!I', packet[4:8])[0]
                sack1_end = struct.unpack('!I', packet[8:12])[0]
                if sack1_start != 0 and sack1_end != 0:
                    sack_blocks.append((sack1_start, sack1_end))
                sack2_start = struct.unpack('!I', packet[12:16])[0]
                sack2_end = struct.unpack('!I', packet[16:20])[0]
                if sack2_start != 0 and sack2_end != 0:
                    sack_blocks.append((sack2_start, sack2_end))
            except:
                pass
        return ack_num, sack_blocks
    def update_rtt(self, sample_rtt):
        if self.estimated_rtt == INITIAL_TIMEOUT:
            self.estimated_rtt = sample_rtt
            self.dev_rtt = sample_rtt / 2
        else:
            self.dev_rtt = (1 - BETA_RTT) * self.dev_rtt + BETA_RTT * abs(sample_rtt - self.estimated_rtt)
            self.estimated_rtt = (1 - ALPHA_RTT) * self.estimated_rtt + ALPHA_RTT * sample_rtt
        self.rto = max(self.estimated_rtt + 4 * self.dev_rtt, 0.2)
        # FIXED: Remove cap if high-delay link; keep for now
        self.rto = min(self.rto, 2.0)
    def send_packet(self, seq_num, data):
        packet = self.make_packet(seq_num, data)
        self.sock.sendto(packet, self.client_addr)
        send_time = time.time()
        self.sent_packets[seq_num] = (data, send_time, 0)
        
        # OPTIMIZATION: Start timer only when sending base packet
        if seq_num == self.base:
            self.timer_start = send_time
    def get_effective_window(self):
        inflight = (self.next_seq_num - self.base) * MSS
        return max(0, int(self.cc.cwnd) - inflight)
    def send_file(self, filename):
        try:
            with open(filename, 'rb') as f:
                file_data = f.read()
        except FileNotFoundError:
            print(f"[Server] File {filename} not found")
            return
        print(f"[Server] Sending file {filename} ({len(file_data)} bytes)")
        chunks = [file_data[i:i + MSS] for i in range(0, len(file_data), MSS)]
        total_chunks = len(chunks)
        self.base = 0
        self.next_seq_num = 0
        last_ack = 0
        start_time = time.time()
        last_print_time = start_time
        
        while self.base < total_chunks:
            current_time = time.time()
            
            # Send new packets within window
            while self.next_seq_num < total_chunks and self.get_effective_window() >= MSS:
                if self.next_seq_num not in self.sent_packets:
                    self.send_packet(self.next_seq_num, chunks[self.next_seq_num])
                self.next_seq_num += 1
            # OPTIMIZATION: Set timeout based on base packet timer only
            if self.timer_start is not None:
                time_elapsed = current_time - self.timer_start
                timeout_remaining = max(self.rto - time_elapsed, 0.01)
                self.sock.settimeout(timeout_remaining)
            else:
                self.sock.settimeout(self.rto)
            
            try:
                ack_packet, addr = self.sock.recvfrom(1024)
                ack_num, sack_blocks = self.parse_ack(ack_packet)
                if ack_num is None:
                    continue
                
                # FIXED: Sample RTT on any new ACK (not just base)
                if ack_num > self.base and any(seq in self.sent_packets for seq in range(ack_num - 1, ack_num)):
                    # Sample from most recent acked if base not available
                    recent_seq = max(seq for seq in range(ack_num - 1, ack_num) if seq in self.sent_packets)
                    send_time = self.sent_packets[recent_seq][1]
                    sample_rtt = time.time() - send_time
                    self.update_rtt(sample_rtt)
                
                # Handle duplicate ACKs (fast retransmit)
                if ack_num == last_ack:
                    if last_ack not in self.dup_ack_count:
                        self.dup_ack_count[last_ack] = 0
                    self.dup_ack_count[last_ack] += 1
                    if self.dup_ack_count[last_ack] == 3:
                        print(f"[Server] Fast retransmit triggered for seq {last_ack}")
                        self.cc.on_loss_detected('fast_retransmit')
                        if last_ack < total_chunks:
                            self.send_packet(last_ack, chunks[last_ack])
                else:
                    # New ACK received - cumulative acknowledgment
                    if ack_num > self.base:
                        bytes_acked = (ack_num - self.base) * MSS
                        self.cc.on_ack(bytes_acked, self.estimated_rtt)
                        
                        # Clear all packets up to ack_num (cumulative ACK)
                        for seq in range(self.base, min(ack_num, total_chunks)):
                            self.sent_packets.pop(seq, None)
                            self.dup_ack_count.pop(seq, None)
                        
                        # Move base forward
                        self.base = ack_num
                        last_ack = ack_num
                        
                        # OPTIMIZATION: Restart timer for new base packet
                        if self.base < total_chunks and self.base in self.sent_packets:
                            self.timer_start = self.sent_packets[self.base][1]
                        else:
                            self.timer_start = None
                # Handle SACK blocks
                for sack_start, sack_end in sack_blocks:
                    for seq in range(sack_start, min(sack_end, total_chunks)):
                        self.sent_packets.pop(seq, None)
            except socket.timeout:
                # FIXED: Selective retransmit on timeout (not GBN)
                if self.base < total_chunks:
                    print(f"[Server] Timeout on base packet (seq {self.base}), selective retransmit")
                    self.cc.on_loss_detected('timeout')
                    # Retransmit only unacked packets
                    for seq in list(self.sent_packets.keys()):
                        if seq < total_chunks:
                            self.send_packet(seq, chunks[seq])
                    # Reset timer after retransmits
                    if self.base in self.sent_packets:
                        self.timer_start = self.sent_packets[self.base][1]
            # Progress logging - FIXED: Use actual bytes
            if current_time - last_print_time > 2.0:
                progress = (self.base / total_chunks) * 100
                elapsed = current_time - start_time
                sent_bytes = sum(len(self.sent_packets.get(seq, (b'',))[0]) for seq in self.sent_packets) if self.sent_packets else 0
                phase = "Slow Start" if self.cc.in_slow_start else "CUBIC"
                t_since_epoch = time.time() - self.cc.epoch_start if self.cc.epoch_start > 0 else 0
                print(f"[Server] Progress: {progress:.1f}%, Phase: {phase}, cwnd={self.cc.cwnd/MSS:.1f}, "
                      f"w_max={self.cc.w_max/MSS:.1f}, K={self.cc.K:.2f}s, t={t_since_epoch:.2f}s, "
                      f"ssthresh={self.cc.ssthresh/MSS:.1f}, RTT={self.estimated_rtt:.3f}s, sent_bytes={sent_bytes}")
                last_print_time = current_time
        # Send EOF
        eof_packet = self.make_packet(total_chunks, b"EOF")
        for _ in range(5):
            self.sock.sendto(eof_packet, self.client_addr)
            time.sleep(0.1)
        elapsed = time.time() - start_time
        throughput = (len(file_data) * 8) / (elapsed * 1e6)
        print(f"[Server] File sent successfully in {elapsed:.2f}s, throughput={throughput:.2f} Mbps")
        print(f"[CUBIC] Final stats: w_max={self.cc.w_max/MSS:.1f}, K={self.cc.K:.3f}s")
    def wait_for_client(self):
        print("[Server] Waiting for client request...")
        try:
            data, addr = self.sock.recvfrom(1024)
            self.client_addr = addr
            print(f"[Server] Received request from {addr}")
            return True
        except socket.timeout:
            return False
    def run(self):
        if self.wait_for_client():
            self.send_file("data.txt")
        self.sock.close()
        print("[Server] Closed")
def main():
    if len(sys.argv) != 3:
        print("Usage: python3 p2_server_corrected.py <SERVER_IP> <SERVER_PORT>")
        sys.exit(1)
    server_ip = sys.argv[1]
    server_port = int(sys.argv[2])
    server = ReliableUDPServer(server_ip, server_port)
    server.run()
if __name__ == "__main__":
    main()