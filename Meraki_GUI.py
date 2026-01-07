import requests
import time
import shutil
import zipfile
import json
import logging
import pathlib
import threading
import tkinter as tk
import sys
from tkinter import filedialog, messagebox, scrolledtext
import re


# --- Backend Functions (Both Meraki and Catalyst) ---

def parse_catalyst_output(file_path, verbose=False):
    """Parses 'show ap summary' for Catalyst."""
    bssids_dict = {}
    models_dict = {'Catalyst': {}}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        lines = content.splitlines()
        for line in lines:
            # OPSU_US_EST_AP113_GALERIA_SUPERI 2     C9124AXD-ROW         10e3.7670.042c 10e3.7626.16e0
            match = re.search(r'^(\S+)\s+\d+\s+(\S+)\s+([0-9a-fA-F\.]{14})\s+([0-9a-fA-F\.]{14})', line.strip())
            if match:
                ap_name = match.group(1)
                model = match.group(2)
                radio_mac = match.group(4).replace('.', '').lower()
                bssids_dict[ap_name] = radio_mac
                models_dict['Catalyst'][ap_name] = model
        return bssids_dict, models_dict
    except Exception as e:
        print(f"Error parsing Catalyst file: {e}")
        return {}, {}


# --- Funções de Backend (Lógica Meraki/Ekahau) ---

def get_data(url, api_key, query=None):
    if query is None:
        query = {}
    headers = {'Content-type': 'application/json',
               'X-Cisco-Meraki-API-Key': api_key}
    try:
        r = requests.get(url, headers=headers, params=query)
        if r.status_code == 429:
            wait_time = int(r.headers.get("Retry-After", 1))
            print(f'Rate limit hit. Waiting {wait_time} seconds...')
            time.sleep(wait_time)
            return get_data(url, api_key, query)
        elif r.status_code == 404:
            print(f'Warning: Resource not found (404) for URL: {url}')
            logging.debug(f'Warning: Resource not found for URL {url}')
            return []
        elif r.status_code != 200:
            print(f'Error {r.status_code}: {r.text}')
            return []
    except requests.exceptions.RequestException as e:
        print(f'Request Exception: {e}')
        return []

    return r.json()


def get_organization_ids(api_key, verbose=False):
    print("Fetching Organizations...")
    orgs = get_data('https://api.meraki.com/api/v1/organizations', api_key)
    orgs_dict = {}

    if not isinstance(orgs, list):
        print("Error fetching organizations. Check API Key.")
        return {}

    for item in orgs:
        orgs_dict[item['name']] = item['id']
        if verbose:
            print(f'ORG: {item["name"]}, ID: {item["id"]}')

    logging.debug(f'Organizations received via API: {orgs_dict}')
    if verbose:
        print('Organizations - Done!')
    return orgs_dict


def get_network_id(api_key, org_id, network_name, verbose=False):
    """Busca o ID da network baseado no nome fornecido dentro de uma organização."""
    url = f'https://api.meraki.com/api/v1/organizations/{org_id}/networks'
    networks = get_data(url, api_key)

    for net in networks:
        if net['name'] == network_name:
            if verbose:
                print(f"Network found: {net['name']} ID: {net['id']}")
            return net['id']

    print(f"Warning: Network '{network_name}' not found in this Organization.")
    return None


def get_organization_aps(api_key, orgs_dict, organization_name='', network_name='', ap_item='serial', verbose=False):
    aps_dict = {}

    # Filtra organização específica se fornecida
    if organization_name:
        if organization_name in orgs_dict:
            orgs_dict = {organization_name: orgs_dict[organization_name]}
            if verbose:
                print(f'Org selected: {organization_name}')
        else:
            print('Provided organization name does not exist.')
            return {}

    for org_name, org_id in orgs_dict.items():
        aps_dict[org_id] = {}

        # Define query base
        query = {'productTypes[]': 'wireless'}

        # Lógica de filtro por Network
        if network_name:
            net_id = get_network_id(api_key, org_id, network_name, verbose)
            if net_id:
                # Se temos o ID da network, filtramos a query da API
                query['networkIds[]'] = net_id
            else:
                # Se a network não for encontrada nesta org, pula para a próxima org ou aborta
                continue

        url = f'https://api.meraki.com/api/v1/organizations/{org_id}/devices/statuses'
        print(f"Fetching devices for Org: {org_name}...")
        aps = get_data(url, api_key, query)

        if aps and isinstance(aps, list):
            count = 0
            for item in aps:
                if 'errors' not in item:  # Dispositivo online/válido
                    # Filtro extra de segurança para garantir que é wireless (caso a API retorne outros)
                    if item.get('productType') == 'wireless':
                        key_val = item.get('serial') if ap_item == 'serial' else item.get('model')
                        aps_dict[org_id][item['name']] = key_val
                        count += 1
                        if verbose:
                            print(f"Linked {item['name']} to {ap_item.title()}: {key_val}")

            print(f"Found {count} APs in {org_name}.")
            logging.debug(f'AP {ap_item} received via API: \n {aps_dict}')
        else:
            print(f"No devices found or error fetching for {org_name}")

    return aps_dict


def get_aps_bssids(api_key, aps_dict, verbose=False):
    bssids_dict = {}
    total_aps = sum(len(v) for v in aps_dict.values())
    print(f"Fetching BSSIDs for {total_aps} APs (this may take a while)...")

    for ap_info in aps_dict.values():
        for ap_name, serial in ap_info.items():
            url = f'https://api.meraki.com/api/v1/devices/{serial}/wireless/status'
            bssids = get_data(url, api_key)

            if 'basicServiceSets' in bssids:
                # Get first enabled BSSID
                for radio in bssids['basicServiceSets']:
                    if radio['enabled']:
                        bssids_dict[ap_name] = radio['bssid']
                        if verbose:
                            print(f'Linked {ap_name} to BSSID {radio["bssid"]}')
                        break

    if verbose:
        print('BSSIDs - Done!')
    logging.debug(f'AP Name - BSSID map: {bssids_dict}')
    return bssids_dict


def add_ap_names(project_filename, bssid_dict, models, change_model=False, verbose=False):
    p = pathlib.Path('Ekahau_Temp/')
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)

    working_directory = pathlib.Path.cwd()
    temp_folder_filepath = working_directory / 'Ekahau_Temp'

    print(f"Processing Ekahau File: {project_filename}")

    try:
        with zipfile.ZipFile(project_filename, 'r') as myzip:
            myzip.extractall(temp_folder_filepath)

        # Carregar JSONs
        with open(temp_folder_filepath / 'accessPoints.json', 'r', encoding='utf-8') as f:
            access_points = json.load(f)
        with open(temp_folder_filepath / 'measuredRadios.json', 'r', encoding='utf-8') as f:
            measured_radios = json.load(f)
        with open(temp_folder_filepath / 'accessPointMeasurements.json', 'r', encoding='utf-8') as f:
            access_point_measurements = json.load(f)

        changes_count = 0

        # --- Parte 1: Atualizar Nomes (Name Mapping) ---
        for ap_name, bssid in bssid_dict.items():
            if not bssid: continue

            # Normalização do BSSID/MAC
            clean_bssid = bssid.replace(':', '').replace('.', '').lower()

            for measurement in access_point_measurements.get('accessPointMeasurements', []):
                raw_mac = measurement.get('mac')
                if not raw_mac: continue

                meas_mac = raw_mac.replace(':', '').lower()

                # Lógica de match flexível
                # Para Meraki costuma-se usar o sufixo, para Catalyst o prefixo é mais garantido
                # Vamos tentar prefixo se tiver 10+ chars ou sufixo se for Meraki original
                match_found = False
                if len(clean_bssid) >= 11:
                    # Catalyst / Generic Prefix Match (best for multiple SSIDs on same AP)
                    if meas_mac.startswith(clean_bssid[:11]):
                        match_found = True
                else:
                    # Fallback or Meraki Suffix Match
                    if clean_bssid[6:] in meas_mac:
                        match_found = True

                if match_found:
                    meas_id = measurement.get('id')

                    for measuredRadio in measured_radios.get('measuredRadios', []):
                        # Verifica se o ID da medição está neste rádio
                        if meas_id in measuredRadio.get('accessPointMeasurementIds', []):

                            ap_id = measuredRadio.get('accessPointId')

                            for ekahau_ap in access_points.get('accessPoints', []):
                                if ekahau_ap.get('id') == ap_id:

                                    # CORREÇÃO DO ERRO 'name':
                                    # Usamos .get() para ler o nome atual com segurança
                                    current_ek_name = ekahau_ap.get('name', 'Unknown-AP')

                                    if current_ek_name != ap_name:
                                        ekahau_ap['name'] = ap_name
                                        changes_count += 1
                                        if verbose:
                                            print(f'Updated AP: {current_ek_name} -> {ap_name}')

        # --- Parte 2: Atualizar Modelos (Model Mapping) ---
        if change_model and models:
            for org_models in models.values():
                for m_name, m_model in org_models.items():
                    for ekahau_ap in access_points.get('accessPoints', []):

                        # Verifica propriedade 'mine' e 'name' com segurança
                        is_mine = ekahau_ap.get('mine', True)
                        current_ek_name = ekahau_ap.get('name', '')

                        if is_mine and current_ek_name == m_name:
                            ekahau_ap['model'] = m_model
                            if verbose:
                                print(f'Updated Model for {m_name}: {m_model}')

        # Salvar Alterações
        with open(temp_folder_filepath / 'accessPoints.json', 'w', encoding='utf-8') as file:
            json.dump(access_points, file, indent=4)

        # Repack e Limpeza final
        new_filename = str(project_filename).replace('.esx', '_modified.esx')
        shutil.make_archive('temp_archive', 'zip', temp_folder_filepath)

        final_path = pathlib.Path(new_filename)
        if final_path.exists():
            final_path.unlink()

        shutil.move('temp_archive.zip', new_filename)

        print(f"\nSUCCESS! File saved as: {new_filename}")
        print(f"Total AP names updated: {changes_count}")

    except Exception as e:
        print(f"Critical Error processing file: {e}")
        logging.error(e)
        import traceback
        traceback.print_exc()  # Isso vai mostrar onde exatamente estourou o erro no console
    finally:
        if p.exists():
            shutil.rmtree(p)

# --- Classes da Interface Gráfica ---

class TextRedirector(object):
    def __init__(self, widget, tag="stdout"):
        self.widget = widget
        self.tag = tag

    def write(self, str):
        self.widget.configure(state="normal")
        self.widget.insert("end", str, (self.tag,))
        self.widget.see("end")
        self.widget.configure(state="disabled")

    def flush(self):
        pass


class AppGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Meraki/Catalyst <-> Ekahau Sync Tool")
        self.root.geometry("600x650")

        # Variáveis
        self.source_mode = tk.StringVar(value="Meraki")
        self.api_key = tk.StringVar()
        self.org_name = tk.StringVar()
        self.net_name = tk.StringVar()
        self.cat_file = tk.StringVar()
        self.file_path = tk.StringVar()
        self.verbose = tk.BooleanVar(value=True)
        self.get_model = tk.BooleanVar(value=False)

        self._build_ui()

    def _build_ui(self):
        # Frame de Seleção de Modo
        mode_frame = tk.LabelFrame(self.root, text="Source System", padx=10, pady=5)
        mode_frame.pack(padx=10, pady=5, fill="x")
        
        tk.Radiobutton(mode_frame, text="Cisco Meraki (API)", variable=self.source_mode, 
                       value="Meraki", command=self._toggle_mode).pack(side="left", padx=20)
        tk.Radiobutton(mode_frame, text="Cisco Catalyst (Text File)", variable=self.source_mode, 
                       value="Catalyst", command=self._toggle_mode).pack(side="left", padx=20)

        # Frame de Inputs
        self.input_frame = tk.LabelFrame(self.root, text="Configuration", padx=10, pady=10)
        self.input_frame.pack(padx=10, pady=5, fill="x")

        # --- Meraki Fields ---
        self.lbl_api = tk.Label(self.input_frame, text="Meraki API Key:")
        self.ent_api = tk.Entry(self.input_frame, textvariable=self.api_key, width=40, show="*")
        
        self.lbl_org = tk.Label(self.input_frame, text="Organization Name:")
        self.ent_org = tk.Entry(self.input_frame, textvariable=self.org_name, width=40)
        
        self.lbl_net = tk.Label(self.input_frame, text="Network Name (Optional):")
        self.ent_net = tk.Entry(self.input_frame, textvariable=self.net_name, width=40)

        # --- Catalyst Fields ---
        self.lbl_cat = tk.Label(self.input_frame, text="Catalyst Output (.txt):")
        self.ent_cat = tk.Entry(self.input_frame, textvariable=self.cat_file, width=30)
        self.btn_cat = tk.Button(self.input_frame, text="Browse", command=self._browse_cat_file)

        # --- Shared Fields ---
        tk.Label(self.input_frame, text="Ekahau File (.esx):").grid(row=4, column=0, sticky="w")
        tk.Entry(self.input_frame, textvariable=self.file_path, width=30).grid(row=4, column=1, sticky="w", padx=5)
        tk.Button(self.input_frame, text="Browse", command=self._browse_file).grid(row=4, column=1, sticky="e")

        # Options
        opt_frame = tk.Frame(self.input_frame)
        opt_frame.grid(row=5, column=0, columnspan=2, pady=10)
        tk.Checkbutton(opt_frame, text="Verbose Log", variable=self.verbose).pack(side="left", padx=10)
        tk.Checkbutton(opt_frame, text="Update AP Models", variable=self.get_model).pack(side="left", padx=10)

        self._toggle_mode() # Set initial layout

        # Action Button
        self.btn_run = tk.Button(self.root, text="START PROCESS", bg="#4CAF50", fg="white", font=("Arial", 10, "bold"),
                                 command=self._start_thread)
        self.btn_run.pack(pady=10, fill="x", padx=20)

        # Console Output
        console_frame = tk.LabelFrame(self.root, text="Logs / Output", padx=5, pady=5)
        console_frame.pack(padx=10, pady=5, fill="both", expand=True)

        self.txt_console = scrolledtext.ScrolledText(console_frame, state="disabled", height=15)
        self.txt_console.pack(fill="both", expand=True)

        # Redirect stdout
        sys.stdout = TextRedirector(self.txt_console)
        # sys.stderr = TextRedirector(self.txt_console) # Opcional: redirecionar erros também

    def _toggle_mode(self):
        """Show/Hide fields based on selected source mode."""
        if self.source_mode.get() == "Meraki":
            # Show Meraki
            self.lbl_api.grid(row=0, column=0, sticky="w")
            self.ent_api.grid(row=0, column=1, padx=5, pady=5)
            self.lbl_org.grid(row=1, column=0, sticky="w")
            self.ent_org.grid(row=1, column=1, padx=5, pady=5)
            self.lbl_net.grid(row=2, column=0, sticky="w")
            self.ent_net.grid(row=2, column=1, padx=5, pady=5)
            # Hide Catalyst
            self.lbl_cat.grid_forget()
            self.ent_cat.grid_forget()
            self.btn_cat.grid_forget()
        else:
            # Hide Meraki
            self.lbl_api.grid_forget()
            self.ent_api.grid_forget()
            self.lbl_org.grid_forget()
            self.ent_org.grid_forget()
            self.lbl_net.grid_forget()
            self.ent_net.grid_forget()
            # Show Catalyst
            self.lbl_cat.grid(row=0, column=0, sticky="w")
            self.ent_cat.grid(row=0, column=1, sticky="w", padx=5, pady=5)
            self.btn_cat.grid(row=0, column=1, sticky="e", pady=5)

    def _browse_cat_file(self):
        filename = filedialog.askopenfilename(filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")])
        if filename:
            self.cat_file.set(filename)

    def _browse_file(self):
        filename = filedialog.askopenfilename(filetypes=[("Ekahau Files", "*.esx")])
        if filename:
            self.file_path.set(filename)

    def _start_thread(self):
        # Validação básica
        mode = self.source_mode.get()
        if mode == "Meraki":
            if not self.api_key.get() or not self.org_name.get():
                messagebox.showwarning("Missing Data", "Please fill API Key and Organization Name.")
                return
        else:
            if not self.cat_file.get():
                messagebox.showwarning("Missing Data", "Please select the Catalyst output file.")
                return
                
        if not self.file_path.get():
            messagebox.showwarning("Missing Data", "Please select an Ekahau (.esx) file.")
            return

        self.btn_run.config(state="disabled", text="Running...")
        # Iniciar worker thread
        t = threading.Thread(target=self._process_logic)
        t.daemon = True
        t.start()

    def _process_logic(self):
        try:
            print("--- Starting Process ---")
            home = pathlib.Path.cwd()

            # Setup Log File
            log_filepath = home / 'Ekahau_GUI.log'
            logging.basicConfig(filename=str(log_filepath), filemode='w', level=logging.DEBUG, force=True)

            mode = self.source_mode.get()
            esx_file = pathlib.Path(self.file_path.get())
            is_verbose = self.verbose.get()
            update_models = self.get_model.get()
            
            bssid_dict = {}
            models_dict = None

            if mode == "Meraki":
                token = self.api_key.get()
                org_filter = self.org_name.get()
                net_filter = self.net_name.get()

                # 1. Get Orgs
                org_ids = get_organization_ids(token, verbose=is_verbose)

                # 2. Get APs
                aps = get_organization_aps(token, org_ids, org_filter, net_filter, verbose=is_verbose)

                if not aps:
                    print("No APs found with current criteria.")
                    return

                # 3. Get BSSIDs
                bssid_dict = get_aps_bssids(token, aps, verbose=is_verbose)
                
                if update_models and bssid_dict:
                    print("Fetching Models...")
                    models_dict = get_organization_aps(token, org_ids, org_filter, net_filter, ap_item='model',
                                                       verbose=is_verbose)
            else:
                # Catalyst Mode
                print(f"Parsing Catalyst file: {self.cat_file.get()}")
                bssid_dict, models_dict = parse_catalyst_output(self.cat_file.get(), verbose=is_verbose)

            if bssid_dict:
                # 4. Modify Ekahau File
                add_ap_names(esx_file, bssid_dict, models_dict, update_models, verbose=is_verbose)
                messagebox.showinfo("Done", "Process Finished Successfully!")
            else:
                print('No AP data retrieved.')
                messagebox.showerror("Error", "No AP data found. Check your inputs.")

        except Exception as e:
            print(f"FATAL ERROR: {e}")
            logging.exception("Fatal Error")
            messagebox.showerror("Error", f"An error occurred:\n{e}")
        finally:
            self.root.after(0, lambda: self.btn_run.config(state="normal", text="START PROCESS"))


def main():
    root = tk.Tk()
    app = AppGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()