#!/usr/bin/env python3

import socket
import argparse
import logging
import time

# Constants
MSS = 1180
TIMEOUT = 1.0
# --- Adaptive ACK Parameters ---
ACK_NORMAL = 5    # N=30 when no loss
ACK_RECOVERY = 1   # N=1 when loss is detected


def receive_file(server_ip, server_port, file_prefix):
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_address = (server_ip, server_port)
    expected_seq_num = 0
    last_acked_seq = -1  # Track last ACKed packet

    output_file_path = f"{file_prefix}received_data.txt"
    connected = False
    buffer = {}

    # --- Adaptive ACK State Variables ---
    ack_every_n = ACK_NORMAL
    packets_since_last_ack = 0

    logging.info(f"Client started, will save to {output_file_path}")

    try:
        with open(output_file_path, 'wb') as file:

            # --- Handshake ---
            while not connected:
                try:
                    logging.info("Sending START")
                    client_socket.sendto(b"START", server_address)
                    client_socket.settimeout(TIMEOUT)
                    packet, _ = client_socket.recvfrom(max(1024, MSS + 100))
                    if packet == b"CONNECT":
                        connected = True
                        logging.info("Connection established")
                        break
                except socket.timeout:
                    logging.info("Timeout waiting for CONNECT, retrying...")
                    time.sleep(0.5)

            # --- Receive Loop ---
            while True:
                try:
                    client_socket.settimeout(TIMEOUT)
                    packet, _ = client_socket.recvfrom(max(1024, MSS + 100))

                    if packet.startswith(b"CONNECT"):
                        continue

                    if packet.startswith(b"END"):
                        logging.info("Received END signal, sending END_ACK")
                        for _ in range(5):
                            client_socket.sendto(b"END_ACK", server_address)
                            time.sleep(0.01)
                        break

                    seq_num, data = parse_packet(packet)
                    if seq_num == -1:
                        continue

                    logging.info(f"Received packet {seq_num}")

                    if seq_num == expected_seq_num:
                        # --- In-order packet ---
                        file.write(data)
                        file.flush()
                        logging.info(f"Wrote packet {seq_num} to file")

                        expected_seq_num += 1
                        packets_since_last_ack += 1
                        loss_recovered = False

                        # --- Process buffered packets ---
                        while expected_seq_num in buffer:
                            logging.info(f"Writing buffered packet {expected_seq_num}")
                            file.write(buffer.pop(expected_seq_num))
                            file.flush()
                            expected_seq_num += 1
                            packets_since_last_ack += 1
                            loss_recovered = True

                        if loss_recovered:
                            # We filled a gap, so loss is "not detected" anymore
                            # Reset to normal ACK rate
                            ack_every_n = ACK_NORMAL
                            logging.info(f"Loss recovered. Setting ACK rate to {ack_every_n}")

                        # --- Send ACK based on adaptive rate ---
                        if (packets_since_last_ack >= ack_every_n or 
                            seq_num == 0 or 
                            loss_recovered):
                            
                            send_ack(client_socket, server_address, expected_seq_num - 1)
                            last_acked_seq = expected_seq_num - 1
                            packets_since_last_ack = 0

                    elif seq_num > expected_seq_num:
                        # --- Out-of-order packet (LOSS DETECTED) ---
                        if seq_num not in buffer:
                            buffer[seq_num] = data
                            logging.info(f"Buffered out-of-order packet {seq_num}")

                        if ack_every_n != ACK_RECOVERY:
                            logging.info(f"Loss detected! Setting ACK rate to {ACK_RECOVERY}")
                            ack_every_n = ACK_RECOVERY

                        # Still send duplicate ACK (important for loss recovery)
                        if expected_seq_num > 0:
                            send_ack(client_socket, server_address, expected_seq_num - 1)

                    elif seq_num < expected_seq_num:
                        # --- Duplicate packet ---
                        logging.info(f"Received duplicate packet {seq_num}, ignoring")
                        # Duplicate ACK still helps sender
                        if expected_seq_num > 0:
                            send_ack(client_socket, server_address, expected_seq_num - 1)

                except socket.timeout:
                    logging.info(f"Timeout. Last expected: {expected_seq_num}")
                    # Re-send last ACK on timeout
                    if expected_seq_num > 0 and last_acked_seq != expected_seq_num - 1:
                        send_ack(client_socket, server_address, expected_seq_num - 1)
                        last_acked_seq = expected_seq_num - 1

    except Exception as e:
        logging.error(f"Error during file transfer: {e}")
        raise
    finally:
        client_socket.close()
        logging.info(f"File transfer complete. Data saved to {output_file_path}")


def parse_packet(packet):
    """Parse 'SEQ|DATA' format"""
    try:
        seq_num, data = packet.split(b'|', 1)
        return int(seq_num), data
    except (ValueError, TypeError) as e:
        logging.error(f"Malformed packet: {e}")
        return -1, b''


def send_ack(client_socket, server_address, seq_num):
    """Send cumulative ACK for seq_num"""
    ack_packet = f"{seq_num + 1}|ACK".encode()
    client_socket.sendto(ack_packet, server_address)
    logging.info(f"Sent ACK for packet {seq_num} (ACK={seq_num + 1})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Reliable file receiver over UDP.')
    parser.add_argument('server_ip', help='IP address of the server')
    parser.add_argument('server_port', type=int, help='Port number of the server')
    parser.add_argument('file_prefix', help='Prefix for the output file')
    args = parser.parse_args()

    logging.basicConfig(
        filename=f"{args.file_prefix}p2_client_log.txt",
        filemode='w',
        level=logging.INFO,
        format='%(asctime)s - %(message)s'
    )

    try:
        receive_file(args.server_ip, args.server_port, args.file_prefix)
    except KeyboardInterrupt:
        logging.info("Client interrupted by user")
    except Exception as e:
        logging.error(f"Client error: {e}")
        raise
