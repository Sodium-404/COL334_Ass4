#!/usr/bin/env python3

import socket
import time
import argparse
import logging
import csv
import math
import sys # Added for sys.exit

# --- NEW: Match P2 requirements ---
MSS = 1180 # From your P1 spec
FILE_PATH = "data.txt" # From your P1 spec
DUP_ACK_THRESHOLD = 3
TIMEOUT = 1.0 # Initial timeout
MIN_TIMEOUT = 0.2
MAX_TIMEOUT = 5.0

# --- CUBIC Parameters ---
BETA_CUBIC = 0.7 # Multiplicative decrease factor
C_CUBIC = 0.4    # CUBIC constant

# --- Setup Logging (as required by P2) ---
try:
    csv_file = open('cc_log.csv', mode='w', newline='')
    writer_csv = csv.writer(csv_file)
    writer_csv.writerow(['time', 'state', 'cwnd', 'ssthresh', 'bytes_in_flight', 'rtt', 'dup_acks'])
except IOError as e:
    print(f"Error opening log file: {e}. Exiting.")
    sys.exit(1)


def convert_file_to_dict():
    """Reads file and splits it into MSS-sized chunks"""
    file_path = FILE_PATH
    words_dict = {}
    seq_num = 0
    
    try:
        with open(file_path, 'rb') as file:
            while True:
                chunk = file.read(MSS)
                if not chunk:
                    break
                words_dict[seq_num] = chunk
                seq_num += 1
        logging.info(f"File split into {len(words_dict)} packets")
        return words_dict
    except FileNotFoundError:
        logging.error(f"Error: {FILE_PATH} not found.")
        print(f"Error: {FILE_PATH} not found. Please create it.")
        return None

def send_file(server_ip, server_port):

    global TIMEOUT
    
    # --- Congestion Control Variables ---
    # Note: cnwd and ss_threshold are in PACKETS, not bytes
    cnwd = 1.0
    ss_threshold = 64.0 # Start high
    state = 'SLOW_START' # 'SLOW_START', 'CONGESTION_AVOIDANCE', 'FAST_RECOVERY'
    
    # --- CUBIC Variables ---
    w_max = cnwd
    t_epoch = time.time() # Time of last congestion event

    # --- RTT Estimation ---
    rtt_alpha = 0.125
    rtt_beta = 0.25
    estimated_rtt = 0.5 # Start with a default
    dev = 0.25
    
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_socket.bind((server_ip, server_port))
    logging.info(f"Server listening on {server_ip}:{server_port}")

    client_address = None

    # --- Handshake (from your code) ---
    while client_address is None:
        try:
            logging.info("Waiting for client connection...")
            data, client_address = server_socket.recvfrom(1024)
            if data == b"START":
                logging.info(f"Client connected from {client_address}")
                server_socket.sendto(b"CONNECT", client_address)
                logging.info("Sent CONNECT to client")
                break
        except Exception as e:
            logging.warning(f"Handshake recv error: {e}")

    # --- Load File ---
    file_dict = convert_file_to_dict()
    if not file_dict:
        return # Exit if file.txt wasn't found
    
    total_packets = len(file_dict)
    
    seq_num = 0
    window_base = 0
    
    # {seq_num: [packet, send_time, retry_count]}
    unacked_packets = {}
    duplicate_ack_count = 0
    last_ack_received = -1
    complete = False
    start_time_for_log = time.time()

    while not complete:
        # --- Log State ---
        log_time = time.time() - start_time_for_log
        bytes_in_flight = len(unacked_packets) * MSS
        # Log cwnd/ssthresh in BYTES
        writer_csv.writerow([
            f"{log_time:.3f}", state, f"{cnwd * MSS:.0f}", 
            f"{ss_threshold * MSS:.0f}", bytes_in_flight,
            f"{estimated_rtt:.3f}", duplicate_ack_count
        ])

        # --- Send data packets ---
        while seq_num < window_base + int(cnwd):
            if seq_num not in file_dict:
                break # Reached end of file
            
            chunk = file_dict[seq_num]
            packet = create_packet(seq_num, chunk)
            
            send_time = time.time()
            if seq_num in unacked_packets:
                # This is a retransmission (from loop, not timeout)
                _, _, trial_rn = unacked_packets[seq_num]
                unacked_packets[seq_num] = (packet, send_time, trial_rn + 1)
            else:
                unacked_packets[seq_num] = (packet, send_time, 0)
                
            server_socket.sendto(packet, client_address)
            logging.info(f"Sent packet {seq_num}")
            seq_num += 1

        # --- Wait for ACKs ---
        try:
            server_socket.settimeout(TIMEOUT)
            ack_packet, _ = server_socket.recvfrom(1024)
            rec_time = time.time()

            if ack_packet == b"START":
                server_socket.sendto(b"CONNECT", client_address)
                continue

            if "ACK" in ack_packet.decode():
                ack_seq_num = get_seq_no_from_ack_pkt(ack_packet)

                if ack_seq_num > last_ack_received:
                    # --- NEW ACK ---
                    logging.info(f"New ACK received for {ack_seq_num}")
                    duplicate_ack_count = 0
                    last_ack_received = ack_seq_num
                    
                    # Update RTT (only on first-time ACK)
                    if ack_seq_num in unacked_packets:
                        original_send_time, retry_count = unacked_packets[ack_seq_num][1:3]
                        if retry_count == 0: # Karn's Algorithm
                            sample_rtt = rec_time - original_send_time
                            estimated_rtt = (1 - rtt_alpha) * estimated_rtt + rtt_alpha * sample_rtt
                            dev = (1 - rtt_beta) * dev + (rtt_beta * abs(sample_rtt - estimated_rtt))
                            TIMEOUT = estimated_rtt + 4 * dev
                            TIMEOUT = max(MIN_TIMEOUT, min(TIMEOUT, MAX_TIMEOUT))

                    # Remove acknowledged packets
                    for seq in list(unacked_packets.keys()):
                        if seq < ack_seq_num + 1:
                            unacked_packets.pop(seq)
                    
                    window_base = ack_seq_num + 1
                    seq_num = max(seq_num, window_base) # Update next packet to send

                    # --- CUBIC Window Update ---
                    if state == 'SLOW_START':
                        cnwd += 1
                        if cnwd >= ss_threshold:
                            state = 'CONGESTION_AVOIDANCE'
                            w_max = cnwd # Set w_max on exit
                            t_epoch = time.time()
                    
                    elif state == 'CONGESTION_AVOIDANCE':
                        t_now = time.time()
                        elapsed = t_now - t_epoch
                        rtt = estimated_rtt if estimated_rtt > 0.01 else 0.1

                        # K = (W_max * (1-beta)/C)^(1/3)
                        K_arg = (w_max * (1 - BETA_CUBIC) / C_CUBIC)
                        K = (abs(K_arg))**(1/3.0) * (-1 if K_arg < 0 else 1)
                        
                        # W_cubic(t) = C(t-K)^3 + W_max
                        W_t = w_max + C_CUBIC * (elapsed - K)**3
                        
                        # Target for t+RTT
                        W_target = w_max + C_CUBIC * (elapsed + rtt - K)**3

                        # TCP-friendly (concave) region check
                        if W_t < w_max:
                            cnwd = w_max
                        
                        if W_target > cnwd:
                            increase = (W_target - cnwd) / cnwd # Proportional increase
                            cnwd += increase
                        else:
                            cnwd += 1.0 / int(cnwd) # Gentle probe

                    elif state == 'FAST_RECOVERY':
                        # New ACK, exit Fast Recovery
                        state = 'CONGESTION_AVOIDANCE'
                        t_epoch = time.time() # Reset CUBIC epoch
                        cnwd = ss_threshold # CUBIC sets cwnd to ssthresh

                else:
                    # --- DUPLICATE ACK ---
                    if state != 'FAST_RECOVERY': # Don't increment in FR
                        duplicate_ack_count += 1
                    logging.info(f"Duplicate ACK {ack_seq_num}, count={duplicate_ack_count}")

                    if duplicate_ack_count >= DUP_ACK_THRESHOLD:
                        if state != 'FAST_RECOVERY':
                            logging.info("Entering Fast Recovery")
                            state = 'FAST_RECOVERY'
                            w_max = cnwd # Save W_max *before* reduction
                            ss_threshold = max(int(cnwd * BETA_CUBIC), 2)
                            cnwd = ss_threshold # CUBIC Multiplicative Decrease
                            t_epoch = time.time() # Reset epoch
                            
                            # Fast Retransmit
                            fast_retransmit(server_socket, client_address, unacked_packets)
                        else:
                            # CUBIC: In Fast Recovery, do nothing on dup ACKs
                            pass 

        except socket.timeout:
            # --- TIMEOUT ---
            logging.info(f"Timeout occurred. State={state}, cwnd={cnwd}")
            state = 'SLOW_START'
            w_max = cnwd # Save W_max
            ss_threshold = max(int(cnwd * BETA_CUBIC), 2)
            cnwd = 1 # Reset to 1
            t_epoch = time.time() # Reset CUBIC epoch
            duplicate_ack_count = 0
            
            # Double timeout
            TIMEOUT = min(TIMEOUT * 2, MAX_TIMEOUT)
            
            # Go Back N logic (from your code)
            if unacked_packets:
                seq_num = min(unacked_packets.keys())
                window_base = seq_num
            else:
                # No unacked packets, but timeout? Maybe on END.
                pass
            
            logging.info(f"Timeout: Resetting window. New ssthresh={ss_threshold}, cnwd={cnwd}")

        # Check for completion
        if window_base == total_packets and len(unacked_packets) == 0:
            logging.info("File transfer complete")
            complete = True
            break
            
    send_end_signal(server_socket, client_address)
    logging.info("Sending End signal complete")


def create_packet(seq_num, data):
    return f"{seq_num}|".encode() + data

def fast_retransmit(server_socket, client_address, unacked_packets):
    """Retransmit the earliest unacknowledged packet"""
    if not unacked_packets:
        return
    earliest_packet = min(unacked_packets.keys())
    packet, t, trial = unacked_packets[earliest_packet]
    server_socket.sendto(packet, client_address)
    unacked_packets[earliest_packet] = (packet, time.time(), trial + 1)
    logging.info(f"Fast Retransmit: packet {earliest_packet}")

def get_seq_no_from_ack_pkt(ack_packet):
    """Extract seq_num from 'SEQ|ACK'"""
    # ACK is for (seq_num + 1), so we subtract 1
    return int(ack_packet.decode().split('|')[0]) - 1

def send_end_signal(server_socket, client_address):
    """Send END signal until END_ACK is received"""
    end_ack_rec = False
    num_timeout = 0
    while not end_ack_rec and num_timeout <= 10:
        try:
            logging.info("Sending END")
            server_socket.settimeout(TIMEOUT)
            server_socket.sendto(b"END", client_address)
            data, _ = server_socket.recvfrom(1024)
            if data == b"END_ACK":
                logging.info("Received END_ACK from client")
                end_ack_rec = True
        except socket.timeout:
            num_timeout += 1
            logging.info("Socket timeout waiting for END_ACK")

# --- Main ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Reliable file transfer server over UDP (CUBIC).')
    parser.add_argument('server_ip', help='IP address of the server')
    parser.add_argument('server_port', type=int, help='Port number of the server')
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(filename=f"p2_server_log.txt", filemode='w', level=logging.INFO, format='%(asctime)s - %(message)s')
    
    # Run the server
    send_file(args.server_ip, args.server_port)
    csv_file.close()
    logging.info("Server shutting down.")
