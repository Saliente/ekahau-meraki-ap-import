import argparse
import shutil
import zipfile
import json
import logging
import pathlib
import re

def parse_catalyst_output(file_path, verbose=False):
    """
    Parses the "show ap summary" output from a text file.
    :param file_path: Path to the text file
    :param verbose: Print findings
    :return: (bssid_dict, models_dict)
    """
    bssids_dict = {}
    models_dict = {'Catalyst': {}}
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        lines = content.splitlines()
        count = 0
        for line in lines:
            # Match AP Name, Slots, Model, Eth MAC, Radio MAC
            # Example: OPSU_US_EST_AP113_GALERIA_SUPERI 2     C9124AXD-ROW         10e3.7670.042c 10e3.7626.16e0
            # Looking for: Name (non-space) then Slots (digits) then Model (non-space) then ETH MAC (xxxx.xxxx.xxxx) then Radio MAC (xxxx.xxxx.xxxx)
            match = re.search(r'^(\S+)\s+\d+\s+(\S+)\s+([0-9a-fA-F\.]{14})\s+([0-9a-fA-F\.]{14})', line.strip())
            if match:
                ap_name = match.group(1)
                model = match.group(2)
                # Normalizing Radio MAC (removing dots)
                radio_mac = match.group(4).replace('.', '').lower()
                
                bssids_dict[ap_name] = radio_mac
                models_dict['Catalyst'][ap_name] = model
                count += 1
                if verbose:
                    print(f'Found AP: {ap_name}, Model: {model}, Base MAC: {radio_mac}')
        
        print(f'Total Catalyst APs found: {count}')
        return bssids_dict, models_dict
    except Exception as e:
        print(f"Error parsing Catalyst file: {e}")
        return {}, {}

def add_ap_names(project_filename, bssid_dict, models, change_model=False, verbose=False):
    """
    Updates Ekahau project file with Catalyst AP names and models.
    """
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

        # Load Ekahau JSON files
        with open(temp_folder_filepath / 'accessPoints.json', 'r', encoding='utf-8') as f:
            access_points = json.load(f)
        with open(temp_folder_filepath / 'measuredRadios.json', 'r', encoding='utf-8') as f:
            measured_radios = json.load(f)
        with open(temp_folder_filepath / 'accessPointMeasurements.json', 'r', encoding='utf-8') as f:
            access_point_measurements = json.load(f)

        changes_count = 0

        # --- Update Names ---
        for cat_name, radio_mac in bssid_dict.items():
            if not radio_mac: continue
            
            # Using first 11 characters for matching (covers small variations in the last hex digit for different SSIDs)
            mac_prefix = radio_mac[:11]

            for measurement in access_point_measurements.get('accessPointMeasurements', []):
                raw_mac = measurement.get('mac')
                if not raw_mac: continue
                # Normalize Ekahau MAC (remove colons)
                meas_mac = raw_mac.replace(':', '').lower()

                # Match based on prefix (usually the first 11 hex characters are identical for an AP's BSSIDs)
                if meas_mac.startswith(mac_prefix):
                    meas_id = measurement.get('id')
                    for measuredRadio in measured_radios.get('measuredRadios', []):
                        if meas_id in measuredRadio.get('accessPointMeasurementIds', []):
                            ap_id = measuredRadio.get('accessPointId')
                            for ekahau_ap in access_points.get('accessPoints', []):
                                if ekahau_ap.get('id') == ap_id:
                                    current_name = ekahau_ap.get('name', 'Unknown')
                                    if current_name != cat_name:
                                        ekahau_ap['name'] = cat_name
                                        changes_count += 1
                                        if verbose:
                                            print(f'Updated AP: {current_name} -> {cat_name}')

        # --- Update Models ---
        if change_model and models:
            for cat_models in models.values():
                for c_name, c_model in cat_models.items():
                    for ekahau_ap in access_points.get('accessPoints', []):
                        if ekahau_ap.get('mine', True) and ekahau_ap.get('name') == c_name:
                            ekahau_ap['model'] = c_model
                            if verbose:
                                print(f'Updated Model for {c_name}: {c_model}')

        # Save changes to JSON
        with open(temp_folder_filepath / 'accessPoints.json', 'w', encoding='utf-8') as file:
            json.dump(access_points, file, indent=4)

        # Re-zip the Ekahau project
        new_filename = str(project_filename).replace('.esx', '_modified.esx')
        shutil.make_archive('temp_archive', 'zip', temp_folder_filepath)

        final_path = pathlib.Path(new_filename)
        if final_path.exists():
            final_path.unlink()

        shutil.move('temp_archive.zip', new_filename)

        print(f"\nSUCCESS! File saved as: {new_filename}")
        print(f"Total AP names updated: {changes_count}")

    except Exception as e:
        print(f"Error processing file: {e}")
        logging.error(e)
    finally:
        if p.exists():
            shutil.rmtree(p)

def main():
    home = pathlib.Path.cwd()
    
    parser = argparse.ArgumentParser(description='Sync Ekahau AP details with Catalyst "show ap summary" informations')
    parser.add_argument('-f', '--file', metavar='catalyst_output.txt', type=str, required=True,
                        help='Text file containing "show ap summary" output')
    parser.add_argument('-e', '--ekahau', metavar='project.esx', type=str, required=True,
                        help='Ekahau project file')
    parser.add_argument('-m', '--model', action='store_true',
                        help='Update AP models in Ekahau')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show detailed processing steps')

    args = parser.parse_args()

    project_filepath = pathlib.Path(args.ekahau)
    if not project_filepath.exists():
        print(f"Ekahau file not found: {args.ekahau}")
        return

    bssids, models = parse_catalyst_output(args.file, verbose=args.verbose)
    
    if bssids:
        add_ap_names(project_filepath, bssids, models, args.model, args.verbose)
    else:
        print("No AP data found in the provided Catalyst file.")

if __name__ == "__main__":
    main()
