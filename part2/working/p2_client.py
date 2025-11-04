#!/usr/bin/env python3

import socket
import argparse
import logging
import time # Added for sleep

# Constants
MSS = 1180 # Match server
TIMEOUT = 1.0 # Initial timeout

def receive_file(server_ip, server_port, file_prefix):

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_address = (server_ip, server_port)
    expected_seq_num = 0
    
    # --- MODIFIED: Match p2_exp.py ---
    output_file_path = f"{file_prefix}received_data.txt"
    # ---
    
    connected = False
    buffer = {} # For out-of-order packets

    logging.info(f"Client started, will save to {output_file_path}")

    with open(output_file_path, 'wb') as file:
        
        # --- Handshake (from your code) ---
        while not connected:
            try:
                logging.info("Sending START")
                client_socket.sendto(b"START", server_address)
                client_socket.settimeout(TIMEOUT)
                packet, _ = client_socket.recvfrom(max(1024, MSS + 100))
                if packet == b"CONNECT":
                    connected = True
                    logging.info("Connection successful")
                    break
            except socket.timeout:
                logging.info("Timeout waiting for CONNECT, retrying...")
                time.sleep(0.5) # Avoid spamming
        
        # --- Receive Loop ---
        while True:
            try:
                client_socket.settimeout(TIMEOUT)
                packet, _ = client_socket.recvfrom(max(1024, MSS + 100))

                if packet.startswith(b"CONNECT"):
                    continue # Ignore duplicate CONNECT

                if packet.startswith(b"END"):
                    logging.info("Received END signal, sending END_ACK")
                    # Send END_ACK multiple times for reliability
                    for _ in range(5):
                        client_socket.sendto(b"END_ACK", server_address)
                    break # Exit receive loop

                seq_num, data = parse_packet(packet)
                logging.info(f"Received packet {seq_num}")

                if seq_num == expected_seq_num:
                    # --- In-order packet ---
                    file.write(data)
                    file.flush()
                    logging.info(f"Wrote packet {seq_num} to file")
                    
                    # Send cumulative ACK
                    send_ack(client_socket, server_address, seq_num)
                    expected_seq_num += 1

                    # Process buffered packets
                    while expected_seq_num in buffer:
                        logging.info(f"Writing buffered packet {expected_seq_num} to file")
                        file.write(buffer.pop(expected_seq_num))
                        file.flush()
                        send_ack(client_socket, server_address, expected_seq_num)
                        expected_seq_num += 1

                elif seq_num > expected_seq_num:
                    # --- Out-of-order packet ---
                    if seq_num not in buffer:
                        buffer[seq_num] = data
                        logging.info(f"Buffered out-of-order packet {seq_num}")
                    # Send duplicate ACK for the last in-order packet
                    send_ack(client_socket, server_address, expected_seq_num - 1)

                elif seq_num < expected_seq_num:
                    # --- Old (duplicate) packet ---
                    logging.info(f"Received duplicate packet {seq_num}")
                    # Send duplicate ACK
                    send_ack(client_socket, server_address, expected_seq_num - 1)

            except socket.timeout:
                logging.info(f"Timeout waiting for data. Last ACK sent for {expected_seq_num - 1}")
                # When timeout, re-send the last ACK
                send_ack(client_socket, server_address, expected_seq_num - 1)

    logging.info(f"File transfer complete. Data saved to {output_file_path}")

def parse_packet(packet):
    """Parse 'SEQ|DATA'"""
    try:
        seq_num, data = packet.split(b'|', 1)
        return int(seq_num), data
    except (ValueError, TypeError):
        logging.error("Received malformed packet")
        return -1, b''

def send_ack(client_socket, server_address, seq_num):
    """Send cumulative ACK for (seq_num)"""
    # ACK packet is for (seq_num + 1), indicating next expected
    ack_packet = f"{seq_num + 1}|ACK".encode()
    client_socket.sendto(ack_packet, server_address)
    logging.info(f"Sent ACK for packet {seq_num} (ACK num {seq_num + 1})")


# --- Main ---
if __name__ == "__main__":
    # --- MODIFIED: Match p2_exp.py ---
    parser = argparse.ArgumentParser(description='Reliable file receiver over UDP.')
    parser.add_argument('server_ip', help='IP address of the server')
    parser.add_argument('server_port', type=int, help='Port number of the server')
    parser.add_argument('file_prefix', help='Prefix for the output file')
    args = parser.parse_args()

    logging.basicConfig(filename=f"{args.file_prefix}p2_client_log.txt", filemode='w', level=logging.INFO, format='%(asctime)s - %(message)s')
    
    receive_file(args.server_ip, args.server_port, args.file_prefix)
