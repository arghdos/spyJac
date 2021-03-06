import os
from argparse import ArgumentParser

import numpy as np


def get_files(directory):
    return [os.path.join(directory, f) for f in os.listdir(directory)
            if f.endswith('.npy') and
            'pasr_out' in f
            and os.path.isfile(os.path.join(directory, f))]


def load(npy_files, directory=None):
    if not npy_files and directory is not None:
        npy_files = get_files(directory)
    data = None
    num_conditions = 0
    for npy in sorted(npy_files):
        state_data = np.load(npy)
        if data is None:
            data = state_data
        else:
            data = np.vstack((data, state_data))
        num_conditions += state_data.shape[0]
        print(num_conditions, data.shape)
    return num_conditions, data


def write(directory, cut=None, num_conditions=None, data=None):
    if not (num_conditions or data):
        npy_files = get_files(directory)
        num_conditions, data = load(npy_files)

    filename = 'data.bin' if cut is None else 'data_eqremoved.bin'
    with open(os.path.join(directory, filename), 'wb') as file:
        # load PaSR data for different pressures/conditions,
        # and save to binary C file
        if num_conditions == 0:
            print('No data found in folder {}, continuing...'.format(directory))
            return 0
        if cut is not None:
            data = data[cut:, :]
        data.tofile(file)
        del data
    return num_conditions


if __name__ == '__main__':
    parser = ArgumentParser(
        description='data bin writer: Convenience script to generate binary '
                    'files from .npy')
    parser.add_argument('-d', '--directory',
                        type=str,
                        required=True,
                        help='The directory containing the .npy files.')
    parser.add_argument('-c', '--cut_off_front',
                        type=int,
                        default=None,
                        required=False,
                        help='The number of conditions to remove from the front '
                             'of the database')
    args = parser.parse_args()
    write(os.path.realpath(os.path.dirname(args.directory)),
          args.cut_off_front)
