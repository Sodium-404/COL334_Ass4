#!/usr/bin/env python3

import socket
import time
import argparse
import logging
import csv
import math
import sys
import threading

# --- Configuration ---
MSS = 1180
FILE_PATH = "data.txt"
DUP_ACK_THRESHOLD = 3
TIMEOUT = 1.0
MIN_TIMEOUT = 0.2
MAX_TIMEOUT = 2.5
ACK_SCALING = 1.0  # Changed from 2.0
# --- CUBIC Parameters ---
BETA_CUBIC = 0.7
C_CUBIC = 0.4

# --- Setup Logging ---
try:
    csv_file = open('cc_log.csv', mode='w', newline='')
    writer_csv = csv.writer(csv_file)
    writer_csv.writerow(['time', 'state', 'cwnd', 'ssthresh', 'bytes_in_flight', 'rtt', 'dup_acks'])
except IOError as e:
    print(f"Error opening log file: {e}. Exiting.")
    sys.exit(1)


def convert_file_to_dict():
    """Reads file and splits it into MSS-sized chunks"""
    words_dict = {}
    seq_num = 0
    
    try:
        with open(FILE_PATH, 'rb') as file:
            while True:
                chunk = file.read(MSS)
                if not chunk:
                    break
                words_dict[seq_num] = chunk
                seq_num += 1
        logging.info(f"File split into {len(words_dict)} packets")
        print(f"File split into {len(words_dict)} packets")
        return words_dict
    except FileNotFoundError:
        logging.error(f"Error: {FILE_PATH} not found.")
        print(f"Error: {FILE_PATH} not found. Please create it.")
        return None


def send_file(server_ip, server_port):
    
    # --- Congestion Control Variables (in PACKETS) ---
    cwnd = 1.0
    ssthresh = 64.0
    state = 'SLOW_START'
    
    # --- CUBIC Variables ---
    w_max = cwnd
    t_epoch = time.time()
    tcp_cwnd = cwnd
    ack_count = 0

    # --- RTT Estimation ---
    rtt_alpha = 0.125
    rtt_beta = 0.25
    estimated_rtt = 0.5
    dev_rtt = 0.25
    timeout_interval = TIMEOUT
    
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_socket.bind((server_ip, server_port))
    logging.info(f"Server listening on {server_ip}:{server_port}")
    print(f"Server listening on {server_ip}:{server_port}")

    client_address = None

    # --- Handshake ---
    while client_address is None:
        try:
            logging.info("Waiting for client connection...")
            print("Waiting for client connection...")
            data, client_address = server_socket.recvfrom(1024)
            if data == b"START":
                logging.info(f"Client connected from {client_address}")
                print(f"Client connected from {client_address}")
                server_socket.sendto(b"CONNECT", client_address)
                logging.info("Sent CONNECT to client")
                break
        except Exception as e:
            logging.warning(f"Handshake recv error: {e}")

    # --- Load File ---
    file_dict = convert_file_to_dict()
    if not file_dict:
        csv_file.close()
        return
    
    total_packets = len(file_dict)
    
    seq_num = 0
    window_base = 0
    next_seq = 0
    
    # {seq_num: [packet, send_time, retry_count]}
    unacked_packets = {}
    duplicate_ack_count = 0
    last_ack_received = -1
    recovery_point = 0
    complete = False
    start_time_for_log = time.time()
    
    # Threading
    lock = threading.Lock()
    running = [True]  # Use list for mutability in closure
    
    def receive_acks():
        """ACK reception thread"""
        nonlocal cwnd, ssthresh, state, w_max, t_epoch, tcp_cwnd, ack_count
        nonlocal duplicate_ack_count, last_ack_received, window_base, next_seq
        nonlocal estimated_rtt, dev_rtt, timeout_interval, recovery_point
        
        server_socket.settimeout(0.1)
        
        while running[0] and not complete:
            try:
                ack_packet, _ = server_socket.recvfrom(1024)
                rec_time = time.time()
                
                if ack_packet == b"START":
                    server_socket.sendto(b"CONNECT", client_address)
                    continue
                
                if b"ACK" not in ack_packet:
                    continue
                
                try:
                    ack_seq_num = int(ack_packet.decode().split('|')[0]) - 1
                except:
                    continue
                
                with lock:
                    if ack_seq_num > last_ack_received:
                        # --- NEW ACK ---
                        newly_acked = ack_seq_num - last_ack_received
                        logging.info(f"New ACK received for {ack_seq_num}")
                        duplicate_ack_count = 0
                        
                        # Update RTT (Karn's Algorithm)
                        if ack_seq_num in unacked_packets:
                            original_send_time, retry_count = unacked_packets[ack_seq_num][1:3]
                            if retry_count == 0:
                                sample_rtt = rec_time - original_send_time
                                estimated_rtt = (1 - rtt_alpha) * estimated_rtt + rtt_alpha * sample_rtt
                                dev_rtt = (1 - rtt_beta) * dev_rtt + rtt_beta * abs(sample_rtt - estimated_rtt)
                                timeout_interval = estimated_rtt + 4 * dev_rtt
                                timeout_interval = max(MIN_TIMEOUT, min(timeout_interval, MAX_TIMEOUT))
                        
                        # Remove acknowledged packets
                        for seq in list(unacked_packets.keys()):
                            if seq <= ack_seq_num:
                                unacked_packets.pop(seq)
                        
                        last_ack_received = ack_seq_num
                        window_base = ack_seq_num + 1
                        next_seq = max(next_seq, window_base)
                        
                        # Exit fast recovery if past recovery point
                        if state == 'FAST_RECOVERY' and ack_seq_num >= recovery_point:
                            state = 'CONGESTION_AVOIDANCE'
                            cwnd = ssthresh
                            tcp_cwnd = ssthresh
                            logging.info(f"Exited Fast Recovery. cwnd={cwnd:.2f}")
                        
                        # Update congestion window
                        elif state == 'SLOW_START':
                            cwnd += newly_acked * ACK_SCALING
                            tcp_cwnd += newly_acked * ACK_SCALING
                            if cwnd >= ssthresh:
                                state = 'CONGESTION_AVOIDANCE'
                                w_max = cwnd
                                t_epoch = time.time()
                                logging.info(f"Transitioned to CA. cwnd={cwnd:.2f}")
                        
                        elif state == 'CONGESTION_AVOIDANCE':
                            # CUBIC update
                            t_now = time.time()
                            elapsed = t_now - t_epoch
                            rtt = max(estimated_rtt, 0.01)
                            
                            # K calculation
                            K_arg = (w_max * (1 - BETA_CUBIC)) / C_CUBIC
                            K = abs(K_arg) ** (1/3.0)
                            if K_arg < 0:
                                K = -K
                            
                            # W_cubic(t)
                            W_cubic = C_CUBIC * ((elapsed - K) ** 3) + w_max
                            
                            # TCP-friendly
                            tcp_cwnd += newly_acked * ACK_SCALING / tcp_cwnd
                            
                            # Use max of CUBIC and TCP-friendly
                            if W_cubic < tcp_cwnd:
                                cwnd = tcp_cwnd
                            else:
                                W_target = C_CUBIC * ((elapsed + rtt - K) ** 3) + w_max
                                if W_target > cwnd:
                                    increase = max((W_target - cwnd) / cwnd, 1.0 / cwnd)
                                    cwnd += increase * newly_acked * ACK_SCALING
                                else:
                                    cwnd += newly_acked * ACK_SCALING / cwnd
                            
                            cwnd = max(cwnd, 1.0)
                            
                            if window_base % 50 == 0:
                                logging.info(f"CA: cwnd={cwnd:.2f}, tcp_cwnd={tcp_cwnd:.2f}")
                    
                    elif ack_seq_num == last_ack_received and ack_seq_num >= 0:
                        # --- DUPLICATE ACK ---
                        if state != 'FAST_RECOVERY':
                            duplicate_ack_count += 1
                        
                        logging.info(f"Duplicate ACK {ack_seq_num}, count={duplicate_ack_count}")
                        
                        if duplicate_ack_count >= DUP_ACK_THRESHOLD:
                            if state != 'FAST_RECOVERY':
                                # Enter Fast Recovery
                                logging.info("Entering Fast Recovery (3 dup ACKs)")
                                state = 'FAST_RECOVERY'
                                w_max = cwnd
                                ssthresh = max(int(cwnd * BETA_CUBIC), 2)
                                cwnd = float(ssthresh + 3)
                                tcp_cwnd = float(ssthresh)
                                t_epoch = time.time()
                                recovery_point = next_seq - 1
                                
                                # Fast Retransmit
                                if unacked_packets:
                                    earliest_seq = min(unacked_packets.keys())
                                    packet, send_time, retry_count = unacked_packets[earliest_seq]
                                    server_socket.sendto(packet, client_address)
                                    unacked_packets[earliest_seq] = (packet, time.time(), retry_count + 1)
                                    logging.info(f"Fast Retransmit: packet {earliest_seq}")
                            else:
                                # Inflate window
                                cwnd += 1
            
            except socket.timeout:
                continue
            except Exception as e:
                if running[0]:
                    logging.error(f"Error in ACK thread: {e}")
    
    # Start ACK receiver thread
    ack_thread = threading.Thread(target=receive_acks)
    ack_thread.daemon = True
    ack_thread.start()
    
    # Main sending loop
    log_counter = 0
    try:
        while not complete:
            with lock:
                # Log state periodically
                log_counter += 1
                if log_counter % 5 == 0:
                    log_time = time.time() - start_time_for_log
                    bytes_in_flight = len(unacked_packets) * MSS
                    writer_csv.writerow([
                        f"{log_time:.3f}", state, f"{cwnd * MSS:.0f}", 
                        f"{ssthresh * MSS:.0f}", bytes_in_flight,
                        f"{estimated_rtt:.3f}", duplicate_ack_count
                    ])
                
                # Check completion
                if window_base >= total_packets and len(unacked_packets) == 0:
                    complete = True
                    logging.info("File transfer complete")
                    print("File transfer complete")
                    break
                
                # Send packets within congestion window
                bytes_in_flight = (next_seq - window_base) * MSS
                cwnd_bytes = int(cwnd * MSS)
                
                while (next_seq < total_packets and 
                       bytes_in_flight + MSS <= cwnd_bytes):
                    
                    chunk = file_dict[next_seq]
                    packet = f"{next_seq}|".encode() + chunk
                    
                    send_time = time.time()
                    if next_seq in unacked_packets:
                        _, _, retry_count = unacked_packets[next_seq]
                        unacked_packets[next_seq] = (packet, send_time, retry_count + 1)
                    else:
                        unacked_packets[next_seq] = (packet, send_time, 0)
                    
                    server_socket.sendto(packet, client_address)
                    logging.info(f"Sent packet {next_seq}")
                    
                    next_seq += 1
                    bytes_in_flight += MSS
                
                # Check timeout on base packet
                if window_base < total_packets and window_base in unacked_packets:
                    packet, send_time, retry_count = unacked_packets[window_base]
                    if send_time and time.time() - send_time > timeout_interval:
                        logging.info(f"Timeout at base={window_base}")
                        print(f"Timeout at base={window_base}")
                        
                        state = 'SLOW_START'
                        w_max = cwnd
                        ssthresh = max(int(cwnd * BETA_CUBIC), 2)
                        cwnd = 1.0
                        tcp_cwnd = 1.0
                        t_epoch = time.time()
                        duplicate_ack_count = 0
                        ack_count = 0
                        
                        # Exponential backoff
                        timeout_interval = min(timeout_interval * 2, MAX_TIMEOUT)
                        
                        # Retransmit base packet
                        server_socket.sendto(packet, client_address)
                        unacked_packets[window_base] = (packet, time.time(), retry_count + 1)
                        logging.info(f"Retransmitted packet {window_base}")
                        
                        # Reset next_seq to base for Go-Back-N
                        next_seq = window_base
            
            time.sleep(0.001)
    
    finally:
        running[0] = False
        time.sleep(0.2)  # Wait for ACK thread
        send_end_signal(server_socket, client_address)
        csv_file.close()
        logging.info("Server shutdown complete")


def send_end_signal(server_socket, client_address):
    """Send END signal until END_ACK is received"""
    end_ack_received = False
    num_timeouts = 0
    print("Sending END signal...")
    while not end_ack_received and num_timeouts <= 10:
        try:
            logging.info("Sending END")
            server_socket.settimeout(1.0)
            server_socket.sendto(b"END", client_address)
            data, _ = server_socket.recvfrom(1024)
            if data == b"END_ACK":
                logging.info("Received END_ACK from client")
                print("Received END_ACK from client")
                end_ack_received = True
        except socket.timeout:
            num_timeouts += 1
            logging.info("Socket timeout waiting for END_ACK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Reliable file transfer server with CUBIC congestion control.')
    parser.add_argument('server_ip', help='IP address of the server')
    parser.add_argument('server_port', type=int, help='Port number of the server')
    args = parser.parse_args()
    
    logging.basicConfig(
        filename=f"p2_server_log.txt", 
        filemode='w', 
        level=logging.INFO, 
        format='%(asctime)s - %(message)s'
    )
    
    try:
        send_file(args.server_ip, args.server_port)
    except KeyboardInterrupt:
        logging.info("Server interrupted by user")
        print("\nServer interrupted by user")
    except Exception as e:
        logging.error(f"Server error: {e}")
        print(f"Server error: {e}")
        raise
    finally:
        if not csv_file.closed:
             csv_file.close()
