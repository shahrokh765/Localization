'''
Select sensor and detect transmitter
'''

import random
import math
import copy
import time
import os
import numpy as np
import pandas as pd
from scipy.stats import multivariate_normal, norm
from sensor import Sensor
from transmitter import Transmitter
from utility import read_config, ordered_insert, power_2_db, power_2_db_, db_2_power, db_2_power_, find_elbow#, print_results
from counter import Counter
from itertools import combinations
from sklearn.cluster import KMeans
from scipy.optimize import nnls
from plots import visualize_sensor_output, visualize_cluster, visualize_localization, visualize_q_prime, visualize_q, visualize_splot, visualize_unused_sensors
from utility import generate_intruders, generate_intruders_2
from skimage.feature import peak_local_max
import itertools
import line_profiler


class Localization:
    '''Multiple transmitter localization

    Attributes:
        config (json):               configurations - settings and parameters
        sen_num (int):               the number of sensors
        grid_len (int):              the length of the grid
        grid_priori (np.ndarray):    the element is priori probability of hypothesis - transmitter
        grid_posterior (np.ndarray): the element is posterior probability of hypothesis - transmitter
        transmitters (list):         a list of Transmitter
        sensors (dict):              a dictionary of Sensor. less than 10% the # of transmitter
        data (ndarray):              a 2D array of observation data
        covariance (np.ndarray):     a 2D array of covariance. each data share a same covariance matrix
        mean_stds (dict):            assume sigal between a transmitter-sensor pair is normal distributed
        subset (dict):               a subset of all sensors
        subset_index (list):         the linear index of sensor in self.sensors
        meanvec_array (np.ndarray):  contains the mean vector of every transmitter, for CUDA
        TPB (int):                   thread per block
        legal_transmitter (list):    a list of legal transmitters
        lookup_table (np.array):     trade space for time on the q function
    '''
    def __init__(self, grid_len, debug=False):
        self.grid_len = grid_len
        self.sen_num  = 0
        self.grid_priori = np.zeros(0)
        self.grid_posterior = np.zeros(0)
        self.transmitters = []                 # transmitters are the hypothesises
        self.intruders = []
        self.sensors = []
        self.sensors_used = np.array(0)
        self.sensors_collect = {}              # precomputed collected sensors
        self.key = '{}-{}'                     # key template for self.sensors_collect
        self.data = np.zeros(0)
        self.covariance = np.zeros(0)
        self.init_transmitters()
        self.set_priori()
        self.means = np.zeros(0)               # negative mean of intruder
        self.means_primary = np.zeros(0)       # negative mean of intruder plus primary
        self.means_all = np.zeros(0)           # negative mean of intruder plus primary plus secondary (all)
        self.means_rescale = np.zeros(0)       # positive mean of either self.means or self.means_rescale
        self.stds = np.zeros(0)                # for tx, sensor pair
        self.subset = {}
        self.subset_index = []
        self.meanvec_array = np.zeros(0)
        self.TPB = 32
        self.primary_trans = []                # introduce the legal transmitters as secondary user in the Mobicom version
        self.secondary_trans = []              # they include primary and secondary
        self.lookup_table_norm = norm(0, 1).pdf(np.arange(0, 39, 0.0001))  # norm(0, 1).pdf(39) = 0
        self.counter = Counter()               # timer
        self.debug  = debug                    # debug mode do visulization stuff, which is time expensive 


    #@profile
    def init_data(self, cov_file, sensor_file, hypothesis_file):
        '''Init everything from collected real data
           1. init covariance matrix
           2. init sensors
           3. init mean and std between every pair of transmitters and sensors
        '''
        cov = pd.read_csv(cov_file, header=None, delimiter=' ')
        del cov[len(cov)]
        #self.covariance = cov.values
        self.covariance = np.zeros(cov.values.shape)
        np.fill_diagonal(self.covariance, 1.)  # std=1 for every sensor NOTE: need to modify three places

        self.sensors = []
        with open(sensor_file, 'r') as f:
            max_gain = 0.5*len(self.transmitters)
            index = 0
            lines = f.readlines()
            for line in lines:
                line = line.split(' ')
                x, y, std, cost = int(line[0]), int(line[1]), float(line[2]), float(line[3])
                self.sensors.append(Sensor(x, y, 1, cost, gain_up_bound=max_gain, index=index))  # uniform sensors
                index += 1
        self.sen_num = len(self.sensors)

        self.means = np.zeros((self.grid_len * self.grid_len, len(self.sensors)))
        self.stds = np.zeros((self.grid_len * self.grid_len, len(self.sensors)))
        with open(hypothesis_file, 'r') as f:
            lines = f.readlines()
            count = 0
            for line in lines:
                line = line.split(' ')
                tran_x, tran_y = int(line[0]), int(line[1])
                #sen_x, sen_y = int(line[2]), int(line[3])
                mean, std = float(line[4]), float(line[5])
                self.means[tran_x*self.grid_len + tran_y, count] = mean  # count equals to the index of the sensors
                self.stds[tran_x*self.grid_len + tran_y, count] = 1      # std = 1 for every sensor
                count = (count + 1) % len(self.sensors)

        #temp_mean = np.zeros(self.grid_len * self.grid_len, )
        for transmitter in self.transmitters:
            tran_x, tran_y = transmitter.x, transmitter.y
            mean_vec = [0] * len(self.sensors)
            for sensor in self.sensors:
                mean = self.means[self.grid_len*tran_x + tran_y, sensor.index]
                mean_vec[sensor.index] = mean
            transmitter.mean_vec = np.array(mean_vec)


    def vary_power(self, powers):
        '''Varing power
        Args:
            powers (list): an element is a number that denote the difference from the default power read from the hypothesis file
        '''
        for tran in self.transmitters:
            tran.powers = powers


    def init_data_from_model(self, num_sensors):
        self.covariance = np.zeros((num_sensors, num_sensors))
        np.fill_diagonal(self.covariance, 1)
        max_gain = 0.5 * len(self.transmitters)

        #diff = (self.grid_len * self.grid_len) // num_sensors
        diff = 3
        count = 0
        for i in range(0, self.grid_len * self.grid_len, diff):
            x = i // self.grid_len
            y = i % self.grid_len
            self.sensors.append(Sensor(x, y, 1, 1, gain_up_bound=max_gain, index=count))
            count += 1

        self.means = np.zeros((self.grid_len * self.grid_len, len(self.sensors)))
        self.stds = np.ones((self.grid_len * self.grid_len, len(self.sensors)))

        for i in range(0, self.grid_len):
            for j in range(0, self.grid_len):
                for sensor in self.sensors:
                    distance = np.sqrt((i - sensor.x) ** 2 + (j - sensor.y) ** 2)
                    #print('distance = ', distance)
                    self.means[i * self.grid_len + j, sensor.index] = 10 - distance * 5
                    self.stds[i * self.grid_len + j, sensor.index] = 1
        print(self.means)
        for transmitter in self.transmitters:
            tran_x, tran_y = transmitter.x, transmitter.y
            mean_vec = [0] * len(self.sensors)
            for sensor in self.sensors:
                mean = self.means[self.grid_len * tran_x + tran_y, sensor.index]
                mean_vec[sensor.index] = mean
            transmitter.mean_vec = np.array(mean_vec)

        # del self.means
        # del self.stds
        print('\ninit done!')


    def set_priori(self):
        '''Set priori distribution - uniform distribution
        '''
        uniform = 1./(self.grid_len * self.grid_len)
        self.grid_priori = np.full((self.grid_len, self.grid_len), uniform)
        self.grid_posterior = np.full((self.grid_len, self.grid_len), uniform)
    

    def init_transmitters(self):
        '''Initiate a transmitter at all locations
        '''
        self.transmitters = [0] * self.grid_len * self.grid_len
        for i in range(self.grid_len):
            for j in range(self.grid_len):
                transmitter = Transmitter(i, j)
                setattr(transmitter, 'hypothesis', i*self.grid_len + j)
                self.transmitters[i*self.grid_len + j] = transmitter


    def setup_primary_transmitters(self, primary_transmitter, primary_hypo_file):
        '''Setup the primary transmitters, then "train" the distribution of them by linearly adding up the milliwatt power
        Args:
            primary_transmitter (list): index of legal primary transmitters
            primary_hypo_file (str):    filename, primary RSS to each sensor
        '''
        print('Setting up primary transmitters...', end=' ')
        self.primary_trans = []
        for trans in primary_transmitter:
            x = self.transmitters[trans].x
            y = self.transmitters[trans].y
            self.primary_trans.append(Transmitter(x, y))

        dic_mean = {}   # (sensor.x, sensor.y) --> [legal_mean1, legal_mean2, ...]
        dic_std  = {}   # (sensor.x, sensor.y) --> []
        for sensor in self.sensors:
            dic_mean[(sensor.x, sensor.y)] = []
            dic_std[(sensor.x, sensor.y)] = sensor.std
            for primary in self.primary_trans:
                pri_index = self.grid_len * primary.x + primary.y
                dic_mean[(sensor.x, sensor.y)].append(self.means[pri_index, sensor.index])

        # y = 20*log10(x)
        # x = 10^(y/20)
        # where y is power in dB and x is the absolute value of iq samples, i.e. amplitude
        # do a addition in the absolute value of iq samples
        with open(primary_hypo_file, 'w') as f:
            for key, value in dic_mean.items():
                amplitudes = np.power(10, np.array(value)/20)
                addition = sum(amplitudes)
                power_db = 20*np.log10(addition)
                f.write('{} {} {} {}\n'.format(key[0], key[1], power_db, dic_std[key]))


    def add_primary(self, primary_hypo_file):
        '''Add primary's RSS to itruder's RSS and save the sum to self.means_primary, write to file optional
        Params:
            primary_hypo_file (str):   filename, primary RSS to each each sensor
            intru_pri_hypo_file (str): filename, primary RSS plus intruder RSS to each each sensor
            write (bool): if True write the sums to intru_pri_hypo_file, if False don't write
        '''
        print('Adding primary...')
        hypothesis_legal = {}
        with open(primary_hypo_file, 'r') as f:
            lines = f.readlines()
            for line in lines:
                line = line.split(' ')
                sen_x = int(line[0])
                sen_y = int(line[1])
                mean  = float(line[2])
                hypothesis_legal[(sen_x, sen_y)] = db_2_power(mean)

        self.means_primary = np.zeros((len(self.transmitters), len(self.sensors)))
        means_amplitute = db_2_power(self.means)
        for trans_index in range(len(self.transmitters)):
            new_means = np.zeros(len(self.sensors))
            for sen_index in range(len(self.sensors)):
                intru_pri_amplitute = means_amplitute[trans_index, sen_index]
                sen_x = self.sensors[sen_index].x
                sen_y = self.sensors[sen_index].y
                lagel_amplitude = hypothesis_legal.get((sen_x, sen_y))
                add_amplitude   = intru_pri_amplitute + lagel_amplitude
                new_means[sen_index] = add_amplitude
            self.means_primary[trans_index, :] = new_means
        self.means_primary = power_2_db(self.means_primary)


    def setup_secondary_transmitters(self, secondary_transmitter, secondary_hypo_file):
        '''Setup the secondary transmitters, then "train" the distribution of them by linearly adding up the milliwatt power
        Args:
            secondary_transmitter (list): index of legal secondary transmitters
            secondary_hypo_file (str):    filename, secondary RSS to each sensor
        '''
        print('Setting up secondary transmitters...', end=' ')
        self.secondary_trans = []                 # a mistake here, forgot to empty it
        for trans in secondary_transmitter:
            x = self.transmitters[trans].x
            y = self.transmitters[trans].y
            self.secondary_trans.append(Transmitter(x, y))

        dic_mean = {}   # (sensor.x, sensor.y) --> [legal_mean1, legal_mean2, ...]
        dic_std  = {}   # (sensor.x, sensor.y) --> []
        for sensor in self.sensors:
            dic_mean[(sensor.x, sensor.y)] = []
            dic_std[(sensor.x, sensor.y)] = sensor.std
            for secondary in self.secondary_trans:
                sec_index = self.grid_len * secondary.x + secondary.y
                dic_mean[(sensor.x, sensor.y)].append(self.means[sec_index, sensor.index])

        # y = 20*log10(x)
        # x = 10^(y/20)
        # where y is power in dB and x is the absolute value of iq samples, i.e. amplitude
        # do a addition in the absolute value of iq samples
        with open(secondary_hypo_file, 'w') as f:
            for key, value in dic_mean.items():
                amplitudes = np.power(10, np.array(value)/20)
                addition = sum(amplitudes)
                power_db = 20*np.log10(addition)
                f.write('{} {} {} {}\n'.format(key[0], key[1], power_db, dic_std[key]))


    def rescale_all_hypothesis(self):
        '''Rescale hypothesis, and save it in a new np.array # TODO
        '''
        threshold = -80
        num_trans = len(self.transmitters)
        num_sen   = len(self.sensors)
        self.means_rescale = np.zeros((num_trans, num_sen))
        for i in range(num_trans):
            for j in range(num_sen):
                mean = self.means_all[i, j]
                mean -= threshold
                mean = mean if mean>=0 else 0
                self.means_rescale[i, j] = mean
                self.transmitters[i].mean_vec[j] = mean

    #@profile
    def add_secondary(self, secondary_file):
        '''Add secondary's RSS to (itruder's plus primary's) RSS and save the sum to self.means_all, write to file optional
        Params:
            secondary_file (str): filename, secondary RSS to each each sensor
            all_file (str):       filename, intruder RSS + primary RSS + secondary RSS to each each sensor
            write (bool):         if True write the sums to all_file, if False don't write
        '''
        print('Adding secondary...')
        hypothesis_legal = {}
        with open(secondary_file, 'r') as f:
            lines = f.readlines()
            for line in lines:
                line = line.split(' ')
                sen_x = int(line[0])
                sen_y = int(line[1])
                mean  = float(line[2])
                hypothesis_legal[(sen_x, sen_y)] = db_2_power(mean)

        self.means_all = np.zeros((len(self.transmitters), len(self.sensors)))
        means_primary_amplitute = db_2_power(self.means_primary)
        for trans_index in range(len(self.transmitters)):
            new_means = np.zeros(len(self.sensors))
            for sen_index in range(len(self.sensors)):
                intru_pri_amplitute = means_primary_amplitute[trans_index, sen_index]
                sen_x = self.sensors[sen_index].x
                sen_y = self.sensors[sen_index].y
                lagel_amplitude = hypothesis_legal.get((sen_x, sen_y))
                add_amplitude   = intru_pri_amplitute + lagel_amplitude
                new_means[sen_index] = add_amplitude
            self.means_all[trans_index, :] = new_means
        self.means_all = power_2_db(self.means_all)


    def rescale_intruder_hypothesis(self):
        '''Rescale hypothesis, and save it in a new np.array
        '''
        threshold = -80
        num_trans = len(self.transmitters)
        num_sen   = len(self.sensors)
        self.means_rescale = np.zeros((num_trans, num_sen))
        for i in range(num_trans):
            for j in range(num_sen):
                mean = self.means[i, j]
                mean -= threshold
                mean = mean if mean>=0 else 0
                self.means_rescale[i, j] = mean
                self.transmitters[i].mean_vec[j] = mean


    def update_subset(self, subset_index):
        '''Given a list of sensor indexes, which represents a subset of sensors, update self.subset
        Args:
            subset_index (list): a list of sensor indexes. guarantee sorted
        '''
        self.subset = []
        self.subset_index = subset_index
        for index in self.subset_index:
            self.subset.append(self.sensors[index])


    def update_transmitters(self):
        '''Given a subset of sensors' index,
           update each transmitter's mean vector sub and multivariate gaussian function
        '''
        for transmitter in self.transmitters:
            transmitter.set_mean_vec_sub(self.subset_index)
            new_cov = self.covariance[np.ix_(self.subset_index, self.subset_index)]
            transmitter.multivariant_gaussian = multivariate_normal(mean=transmitter.mean_vec_sub, cov=new_cov)


    def update_mean_vec_sub(self, subset_index):
        '''Given a subset of sensors' index,
           update each transmitter's mean vector sub
        Args:
            subset_index (list)
        '''
        for transmitter in self.transmitters:
            transmitter.set_mean_vec_sub(subset_index)


    def covariance_sub(self, subset_index):
        '''Given a list of index of sensors, return the sub covariance matrix
        Args:
            subset_index (list): list of index of sensors. should be sorted.
        Return:
            (np.ndarray): a 2D sub covariance matrix
        '''
        sub_cov = self.covariance[np.ix_(subset_index, subset_index)]
        return sub_cov


    def update_battery(self, selected, energy=1):
        '''Update the battery of sensors
        Args:
            energy (int):    energy consumption amount
            selected (list): list of index of selected sensors
        '''
        for select in selected:
            self.sensors[select].update_battery(energy)


    def index_to_sensor(self, index):
        '''A temporary solution for the inappropriate data structure for self.sensors
        '''
        i = 0
        for sensor in self.sensors:
            if i == index:
                return sensor
            else:
                i += 1



    def collect_sensors_in_radius_precompute(self, radius):
        '''For every location, collect sensors within a radius, store them. Avoid computing over and over again
        Args:
            radius (list<int>)
        '''
        for r in radius:
            for l in range(len(self.transmitters)):
                x = self.transmitters[l].x
                y = self.transmitters[l].y
                subset_sensors = self.collect_sensors_in_radius(r, Sensor(x, y))
                self.sensors_collect[self.key.format(l, r)] = subset_sensors


    #@profile
    def collect_sensors_in_radius(self, radius, sensor, given_sensors = None):
        '''Returns a subset of sensors that are within a radius of given sensor'''
        if given_sensors is None:
            given_sensors = self.sensors
        subset_sensors = []
        for cur_sensor in given_sensors:
            if (cur_sensor.x > sensor.x - radius) and (cur_sensor.x < sensor.x + radius) and (cur_sensor.y > sensor.y - radius) and (cur_sensor.y < sensor.y + radius):
                distance_euc = math.sqrt((cur_sensor.x - sensor.x)**2 + (cur_sensor.y - sensor.y)**2)
                if (distance_euc < radius):
                    subset_sensors.append(cur_sensor.index)
        return subset_sensors


    def delete_transmitter(self, trans_pos, power, sensor_subset, sensor_outputs):
        '''Remove a transmitter and change sensor outputs accordingly'''
        trans_index = trans_pos[0] * self.grid_len + trans_pos[1]
        for sen_index in sensor_subset:
            #sensor_output = db_2_amplitude(sensor_outputs[sen_index])
            #sensor_output_from_transmitter = db_2_amplitude(self.means[trans_index, sen_index])
            #sensor_output -= sensor_output_from_transmitter
            #sensor_outputs[sen_index] = amplitude_2_db(sensor_output)
            sensor_output = db_2_power_(sensor_outputs[sen_index])
            #if sen_index == 6:
            #    print(sensor_output)
            sensor_output_from_transmitter = db_2_power_(self.means[trans_index, sen_index] + power)
            sensor_output -= sensor_output_from_transmitter
            sensor_outputs[sen_index] = power_2_db_(sensor_output)
            # if sen_index == 46 or sen_index == 102:
            #     print('-', trans_pos, power, sensor_output_from_transmitter, sensor_output, sensor_outputs[sen_index])
        sensor_outputs[np.isnan(sensor_outputs)] = -120


    def set_intruders(self, true_indices, powers, randomness = False):
        '''Create intruders and return sensor outputs accordingly
        Args:
            true_indices (list): a list of integers (transmitter index)
        Return:
            (list<Transmitters>, np.array<float>): a list of true transmitters and np.array of sensor outputs
        '''
        true_transmitters = []
        for i in true_indices:
            true_transmitters.append(self.transmitters[i])

        sensor_outputs = np.zeros(len(self.sensors))
        for i in range(len(true_transmitters)):
            tran_x = true_transmitters[i].x
            tran_y = true_transmitters[i].y
            power = powers[i]                                # varies power
            for sen_index in range(len(self.sensors)):
                if randomness:
                    dBm = db_2_power_(np.random.normal(self.means[tran_x * self.grid_len + tran_y, sen_index] + power, self.sensors[sen_index].std))
                else:
                    dBm = db_2_power_(self.means[tran_x * self.grid_len + tran_y, sen_index] + power)
                sensor_outputs[sen_index] += dBm
                #if sen_index == 182:
                #    print('+', (tran_x, tran_y), power, dBm, sensor_outputs[sen_index])
        sensor_outputs = power_2_db_(sensor_outputs)
        return (true_transmitters, sensor_outputs)


    def cluster_localization(self, intruders, sensor_outputs, num_of_intruders):
        '''A baseline clustering localization method
        Args:
            intruders (list): a list of integers (transmitter index)
            sensor_outputs (list): a list of float (RSSI)
            num_of_intruders:
        Return:
            (list): a list of locations [ [x1, y1], [x2, y2], ... ]
        '''
        threshold = int(0.25*len(sensor_outputs))       # threshold: instead of a specific value, it is a percentage of sensors
        arg_decrease = np.flip(np.argsort(sensor_outputs))
        threshold = sensor_outputs[arg_decrease[threshold]]
        threshold = threshold if threshold > -70 else -70
        #visualize_sensor_output(self.grid_len, intruders, sensor_outputs, self.sensors, threshold)

        sensor_to_cluster = []
        for index, output in enumerate(sensor_outputs):
            if output > threshold:
                sensor_to_cluster.append((self.sensors[index].x, self.sensors[index].y))
        k = 1
        inertias = []
        upper_bound = min(2*num_of_intruders, num_of_intruders+5)
        while k <= len(sensor_to_cluster) and k <= upper_bound:    # try all K, up to # of sensors above a threshold
            kmeans = KMeans(n_clusters=k).fit(sensor_to_cluster)
            inertias.append(kmeans.inertia_)  # inertia is the sum of squared distances of samples to their closest cluster center
            #visualize_cluster(self.grid_len, intruders, sensor_to_cluster, kmeans.labels_)
            k += 1

        k = find_elbow(inertias, num_of_intruders)              # the elbow point is the best K
        print('Real K = {}, clustered K = {}'.format(len(intruders), k))
        kmeans = KMeans(n_clusters=k).fit(sensor_to_cluster)
        #visualize_cluster(self.grid_len, intruders, sensor_to_cluster, kmeans.labels_)

        localize = kmeans.cluster_centers_
        for i in range(len(localize)):
            localize[i][0] = round(localize[i][0])
            localize[i][1] = round(localize[i][1])
        return localize


    def compute_error2(self, true_locations, pred_locations):
        '''Given the true location and localization location, computer the error
           Comparing to compute_error, this one do not include error, ** used for SPLOT and clustering **
        Args:
            true_locations (list): an element is a tuple (true transmitter 2D location)
            pred_locations (list): an element is a tuple (predicted transmitter 2D location)
        Return:
            (tuple): (distance error, miss, false alarm)
        '''
        if len(pred_locations) == 0:
            return [], 1, 0
        distances = np.zeros((len(true_locations), len(pred_locations)))
        for i in range(len(true_locations)):
            for j in range(len(pred_locations)):
                distances[i, j] = np.sqrt((true_locations[i][0] - pred_locations[j][0]) ** 2 + (true_locations[i][1] - pred_locations[j][1]) ** 2)

        k = 0
        matches = []
        misses = list(range(len(true_locations)))
        falses = list(range(len(pred_locations)))
        while k < min(len(true_locations), len(pred_locations)):
            min_error = np.min(distances)
            min_error_index = np.argmin(distances)
            i = min_error_index // len(pred_locations)
            j = min_error_index %  len(pred_locations)
            matches.append((i, j, min_error))
            distances[i, :] = np.inf
            distances[:, j] = np.inf
            k += 1

        errors = []              # distance error
        detected = 0
        threshold = self.grid_len / 5
        for match in matches:
            error = match[2]
            if error <= threshold:
                errors.append(error)
                detected += 1
                misses.remove(match[0])
                falses.remove(match[1])

        print('\nPred:', end=' ')
        for match in matches:
            print(str(pred_locations[match[1]]).ljust(9), end='; ')
        print('\nTrue:', end=' ')
        for match in matches:
            print(str(true_locations[match[0]]).ljust(9), end='; ')
        print('\nMiss:', end=' ')
        for miss in misses:
            print(true_locations[miss], end=' ')
        print('\nFalse Alarm:', end=' ')
        for false in falses:
            print(pred_locations[false], end=' ')
        print()
        try:
            return errors, (len(true_locations) - detected) / len(true_locations), (len(pred_locations) - detected) / len(true_locations)
        except:
            return [], 0, 0


    def compute_error(self, true_locations, true_powers, pred_locations, pred_powers):
        '''Given the true location and localization location, computer the error **for our localization**
        Args:
            true_locations (list): an element is a tuple (true transmitter 2D location)
            true_powers (list):    an element is a float 
            pred_locations (list): an element is a tuple (predicted transmitter 2D location)
            pred_powers (list):    an element is a float
        Return:
            (tuple): (list, float, float, list), (distance error, miss, false alarm, power error)
        '''
        if len(pred_locations) == 0:
            return [], 1, 0, []
        distances = np.zeros((len(true_locations), len(pred_locations)))
        for i in range(len(true_locations)):
            for j in range(len(pred_locations)):
                distances[i, j] = np.sqrt((true_locations[i][0] - pred_locations[j][0]) ** 2 + (true_locations[i][1] - pred_locations[j][1]) ** 2)

        k = 0
        matches = []
        misses = list(range(len(true_locations)))
        falses = list(range(len(pred_locations)))
        while k < min(len(true_locations), len(pred_locations)):
            min_error = np.min(distances)
            min_error_index = np.argmin(distances)
            i = min_error_index // len(pred_locations)
            j = min_error_index %  len(pred_locations)
            power_error = pred_powers[j] - true_powers[i]
            matches.append((i, j, min_error, power_error))
            distances[i, :] = np.inf
            distances[:, j] = np.inf
            k += 1

        errors = []              # distance error
        power_errors = []         # power error
        detected = 0
        threshold = self.grid_len / 5
        for match in matches:
            error = match[2]
            if error <= threshold:
                errors.append(error)
                power_errors.append(match[3])
                detected += 1
                misses.remove(match[0])
                falses.remove(match[1])

        print('\nPred:', end=' ')
        for match in matches:
            print(str(pred_locations[match[1]]).ljust(9) + str(round(pred_powers[match[1]], 3)).ljust(6), end='; ')
        print('\nTrue:', end=' ')
        for match in matches:
            print(str(true_locations[match[0]]).ljust(9) + str(round(true_powers[match[0]], 3)).ljust(6), end='; ')
        print('\nMiss:', end=' ')
        for miss in misses:
            print(true_locations[miss], true_powers[miss], end=';  ')
        print('\nFalse Alarm:', end=' ')
        for false in falses:
            print(pred_locations[false], pred_powers[false], end=';  ')
        print()
        try:
            return errors, (len(true_locations) - detected) / len(true_locations), (len(pred_locations) - detected) / len(true_locations), power_errors
        except:
            return [], 0, 0, []


    def ignore_boarders(self, edge):
        '''
        Args:
            edge (int): this amount of edge is ignored at the boarders
        '''
        self.grid_priori[0:self.grid_len*edge] = self.grid_priori[-self.grid_len*edge:-1] = 0      # horizontal edge
        for i in range(edge):
            self.grid_priori[np.ix_(range(i, self.grid_len * self.grid_len, self.grid_len))] = 0  # vertical edge
            self.grid_priori[np.ix_(range(self.grid_len - 1 - i, self.grid_len * self.grid_len, self.grid_len))] = 0


    def get_q_threshold(self, radius):
        '''Different number of sensors (expected) get a different thershold
        '''
        inside = self.sen_num * 3.14159 * radius**2 / len(self.transmitters)
        outside = self.sen_num - inside
        #q = np.power(norm(0, 1).pdf(3), inside) * np.power(0.2, outside) * np.power(3., self.sen_num)
        q = np.power(norm(0, 1).pdf(2.77), inside)
        q *= np.power(0.6, outside)  # 0.6 = 0.2 x 3
        q *= np.power(3, inside)
        return q


    def get_q_threshold_custom(self, inside, radius):
        '''Different number of sensors (real) get a different thershold
        Args:
            inside (int): real number of sensors inside radius R
        Return:
            (float): the customized q threshold
        '''
        prior = 1./(3.14 * radius**2)
        outside = self.sen_num - inside
        q = np.power(norm(0, 1).pdf(1.9), inside) * prior  # [1.5, 2] change smaller because of change of db - power ratio function
        q *= np.power(0.6, outside)
        q *= np.power(3, inside)
        return q


    #@profile
    def prune_hypothesis(self, hypotheses, sensor_outputs, radius, least_num_sensor=3):
        '''Prune hypothesis who has less than 3 sensors with RSS > -80 in radius
        Args:
            transmitters (list): a list of candidate transmitter (hypothesis, location)
            sensor_outputs (list)
            radius (int)
        Return:
            (list): an element is a transmitter index (int)
        '''
        prunes = []
        for tran in hypotheses:
            counter = 0
            subset_sensors = self.sensors_collect[self.key.format(tran, radius)]
            for sensor in subset_sensors:
                if sensor_outputs[sensor] > -80:
                    counter += 1
                if counter == least_num_sensor:
                    break
            else:
                prunes.append(tran)
        for prune in prunes:
            hypotheses.remove(prune)


    def prune_hypothesis1_1(self, hypotheses, edge, previous_identified, previous_identified_radius):
        '''Pruning for procedure 1.1
        Args:

        '''
        prunes = []
        for hypo in hypotheses:
            x = hypo//self.grid_len
            y = hypo%self.grid_len
            if x < edge or x > self.grid_len-edge or y < edge or y > self.grid_len-edge:
                prunes.append(hypo)
        for intru, R in zip(previous_identified, previous_identified_radius):
            x = intru[0]
            y = intru[1]
            x_low  = max(0, x-R)
            x_high = min(self.grid_len, x+R)
            y_low  = max(0, y-R)
            y_high = min(self.grid_len, y+R)
            for i in range(x_low, x_high):
                for j in range(y_low, y_high):
                    temp_hypo = i*self.grid_len+j
                    if temp_hypo in hypotheses and math.sqrt((x-i)**2 + (y-j)**2) < R:
                        prunes.append(temp_hypo)
        prunes = np.unique(prunes)
        for prune in prunes:
            hypotheses.remove(prune)
            

    def ignore_screwed_sensor(self, subset_sensors, previous_identified, min_dist):
        '''When a sensor is close to a identified intruder, it is likely to get screwed due to deleting signals with a wrong power
           Ignore the potential screwed sensor whose distance <= min_dist
        Args:
            subset_sensors (list)
            previous_identified (list): an element is a 2D index of intuder
            min_dist (int): threshold of being a likely screwed sensor
        '''
        screwed = []
        for sen in subset_sensors:
            sen_x = self.sensors[sen].x
            sen_y = self.sensors[sen].y
            for intru in previous_identified:
                dist = math.sqrt((sen_x - intru[0])**2 + (sen_y - intru[1])**2)
                if dist <= min_dist:
                    screwed.append(sen)
                    break
        for screw in screwed:
            subset_sensors.remove(screw)


    def mle_closedform(self, sensor_outputs, mean_vec, variance):
        '''Solve the vaires power issue: from discrete values to continueous values
        Args:
            sensor_outputs (np.array): sensor outputs, the data D={x1, x2, ... , xn} of MLE
            mean_vec (np.array):       the mean of the guassian distributions
            variance (np.array):       the variance of sensors (inside a circle)
        Return:
            delta_p (float):  the power
        '''
        prod = np.prod(variance)
        tmp = prod/variance
        delta_p = np.sum(tmp*(sensor_outputs - mean_vec))/(np.sum(tmp))   # closed form solution by doing derivatation on the MLE expresstion
        if delta_p > 2:
            delta_p = 2
        elif delta_p < -2:
            delta_p = -2
        return delta_p


    #@profile
    def posterior_iteration(self, hypotheses, radius, sensor_outputs, fig, previous_identified, subset_index = None):
        '''
        Args:
            hypothesis (list): an element is potential hypothesis
            radius (int): the transmission radius
            sensor_outputs (list): a list of residual RSS of each sensor
            fig (int): for plotting
            previous_identified (list): an element is a 2D index, identified intruder in previous
            subset_index (list): a list of sensor index
        Return:
            posterior (np.array): 1D array of posterior
            H_0 (bool): whether H_0 is the largest likelihood or not
            q (np.array): 2D array of Q
            power_grid (np.array): 2D array of power
        '''
        position_to_check = []
        self.grid_posterior = np.zeros(self.grid_len * self.grid_len + 1)
        power_grid = np.zeros((self.grid_len, self.grid_len))
        out_prob = 0.2 # probability of sensor outside the radius
        constant = 3
        self.prune_hypothesis(hypotheses, sensor_outputs, radius)
        for trans in self.transmitters: #For each location, first collect sensors in vicinity
            if self.grid_priori[trans.x * self.grid_len + trans.y] == 0 or trans.hypothesis not in hypotheses:
                self.grid_posterior[trans.x * self.grid_len + trans.y] = 0
                continue
            if (trans.x, trans.y) in position_to_check:
                print(trans.x, trans.y)
            subset_sensors = self.sensors_collect[self.key.format(trans.hypothesis, radius)]
            self.ignore_screwed_sensor(subset_sensors, previous_identified, min_dist=2)
            subset_sensors = np.array(subset_sensors)
            if len(subset_sensors) < 3:
                likelihood = 0
                #power_max = 0
                delta_p = 0
            else:
                sensor_outputs_copy = np.copy(sensor_outputs)  # change copy to np.array
                sensor_outputs_copy = sensor_outputs_copy[subset_sensors]
                mean_vec = np.copy(trans.mean_vec)
                mean_vec = mean_vec[subset_sensors]
                variance = np.diagonal(self.covariance)[subset_sensors]
                delta_p = self.mle_closedform(sensor_outputs_copy, mean_vec, variance)
                mean_vec = mean_vec + delta_p  # add the delta of power
                stds = np.sqrt(np.diagonal(self.covariance)[subset_sensors])
                array_of_pdfs = self.get_pdfs(mean_vec, stds, sensor_outputs_copy)
                likelihood = np.prod(array_of_pdfs)

                '''
                likelihood_max = 0
                power_max = 0

                for power in trans.powers:                       # varies power
                    sensor_outputs_copy = np.copy(sensor_outputs)
                    sensor_outputs_copy = sensor_outputs_copy[subset_sensors]
                    mean_vec = np.copy(trans.mean_vec)
                    mean_vec = mean_vec[subset_sensors] + power  # add the delta of power
                    stds = np.sqrt(np.diagonal(self.covariance)[subset_sensors])
                    array_of_pdfs = self.get_pdfs(mean_vec, stds, sensor_outputs_copy)
                    likelihood = np.prod(array_of_pdfs)
                    if likelihood > likelihood_max:
                        likelihood_max = likelihood
                        power_max = power
                    if len(np.unique(trans.powers)) == 1:        # no varying power
                        break
                likelihood = likelihood_max
                '''

            likelihood *= np.power(out_prob*constant, len(self.sensors) - len(subset_sensors)) * np.power(constant, len(subset_sensors))

            self.grid_posterior[trans.x * self.grid_len + trans.y] = likelihood * self.grid_priori[trans.x * self.grid_len + trans.y]# don't care about
            power_grid[trans.x][trans.y] = delta_p
            #power_grid[trans.x][trans.y] = power_max

        # Also check the probability of no transmitter to avoid false alarms
        mean_vec = np.full(len(sensor_outputs), -80)
        sensor_outputs_copy = copy.copy(sensor_outputs)
        sensor_outputs_copy[sensor_outputs_copy < -80] = -80
        array_of_pdfs = self.get_pdfs(mean_vec, np.sqrt(np.diagonal(self.covariance)), sensor_outputs_copy)
        likelihood = np.prod(array_of_pdfs) * np.power(2., len(self.sensors))
        self.grid_posterior[self.grid_len * self.grid_len] = likelihood * self.grid_priori[-1]
        # check if H_0's likelihood*prior is one of the largest
        if self.grid_posterior[len(self.transmitters)] == self.grid_posterior[np.argmax(self.grid_posterior)]:
            H_0 = True
        else:
            H_0 = False

        q = copy.copy(self.grid_posterior)
        if self.debug:
            visualize_q(self.grid_len, q, fig)

        grid_posterior_copy = np.copy(self.grid_posterior)
        for trans in self.transmitters:
            if self.grid_posterior[trans.x * self.grid_len + trans.y] == 0:
                continue
            if (trans.x, trans.y) in position_to_check:
                pass #print(self.grid_posterior[trans.x * self.grid_len + trans.y])
            min_x = int(max(0, trans.x - radius))
            max_x = int(min(trans.x + radius, self.grid_len - 1))
            min_y = int(max(0, trans.y - radius))
            max_y = int(min(trans.y + radius, self.grid_len - 1))
            den = np.sum(np.array([self.grid_posterior[x * self.grid_len + y] for x in range(min_x, max_x + 1) for y in range(min_y, max_y + 1)
                                                                              if math.sqrt((x-trans.x)**2 + (y-trans.y)**2) < radius]))
            grid_posterior_copy[trans.x * self.grid_len + trans.y] /= den

        grid_posterior_copy = np.nan_to_num(grid_posterior_copy)
        self.grid_posterior = grid_posterior_copy
        return self.grid_posterior, H_0, q, power_grid


    def reset(self):
        '''Reset some members for our localization
        '''
        self.sensors_used = np.zeros(self.sen_num, dtype=bool)
        self.sensors_collect = {}


    #@profile
    def our_localization(self, sensor_outputs, intruders, fig):
        '''Our localization, reduce R procedure 1 + procedure 2
        Args:
            sensor_outputs (np.array): the RSS of the sensors, information used to do localization
            intruders (list): location of intruders, used only for visualization
            fig (int):        used for log's filenames
        '''
        self.reset()
        identified   = []
        identified_radius = []
        pred_power   = []
        print('Procedure 1')
        self.counter.time1_start()
        R_list = [8, 6, 5, 4]
        self.collect_sensors_in_radius_precompute(R_list)
        hypotheses = list(range(len(self.transmitters)))
        for R in R_list:
            identified_R, pred_power_R = self.procedure1(hypotheses, sensor_outputs, intruders, fig, R, identified)
            identified.extend(identified_R)
            identified_radius.extend([R]*len(identified_R))
            pred_power.extend(pred_power_R)
            self.counter.proc_1 += len(identified_R)
        self.counter.time1_end()

        print('\nProcedure 1.1')
        self.counter.time2_start()
        hypotheses = list(range(len(self.transmitters)))
        for R in R_list:
            identified_R, pred_power_R = self.procedure1_1(hypotheses, sensor_outputs, intruders, fig, R, identified, identified_radius)
            identified.extend(identified_R)
            pred_power.extend(pred_power_R)
            self.counter.proc_1_1 += len(identified_R)
        self.counter.time2_end()

        print('Procedure 2')
        identified2, pred_power2 = self.procedure2(sensor_outputs, intruders, fig, R=6, previous_identified=identified)
        identified.extend(identified2)
        pred_power.extend(pred_power2)

        return identified, pred_power


    def procedure1_1(self, hypotheses, sensor_outputs, intruders, fig, R, previous_identified, previous_identified_radius):
        '''Our hypothesis-based localization algorithm's procedure 1_1
           The key here is locate transmitters with unused sensors (sensors not being deleted)
           and neglect Q' (thus is MLE-based, not MAP-based)
        Args:
            hypotheses (list): transmitters (1D index) that has 2 or more sensors in radius with RSS > -80 
            sensor_outputs (np.array)
            intruders (list): for plotting
            fig (int)       : for plotting
            R (int)
            previous_identified (list): list<(a, b)>
        Return:
            (list<(int, int)>, list<float>): list of predicted locations and powers
        '''
        print('R =', R)
        identified, pred_power = [], []
        position_to_check = []
        q_relative = np.zeros((self.grid_len, self.grid_len))
        power_grid = np.zeros((self.grid_len, self.grid_len))
        if self.debug:
            visualize_unused_sensors(self.grid_len, intruders, sensor_outputs, self.sensors, self.sensors_used, previous_identified, -80, fig)
        self.prune_hypothesis(hypotheses, sensor_outputs, R, least_num_sensor=3)
        self.prune_hypothesis1_1(hypotheses, 2, previous_identified, previous_identified_radius)
        for hypo in hypotheses:
            trans = self.transmitters[hypo]
            if (trans.x, trans.y) in position_to_check:
                print(trans.x, trans.y)
            subset_sensors = self.sensors_collect[self.key.format(hypo, R)]
            unused_subset_sensors = []
            for sen in subset_sensors:
                if self.sensors_used[sen] == False:
                    unused_subset_sensors.append(sen)     # collect sensors in radius that is UNUSED
            if len(unused_subset_sensors) >= 3:
                sensor_outputs_copy = np.copy(sensor_outputs)  # change copy to np.array
                sensor_outputs_copy = sensor_outputs_copy[unused_subset_sensors]
                mean_vec = np.copy(trans.mean_vec)
                mean_vec = mean_vec[unused_subset_sensors]
                variance = np.diagonal(self.covariance)[unused_subset_sensors]
                delta_p = self.mle_closedform(sensor_outputs_copy, mean_vec, variance)
                mean_vec = mean_vec + delta_p  # add the delta of power
                stds = np.sqrt(np.diagonal(self.covariance)[unused_subset_sensors])
                array_of_pdfs = self.get_pdfs(mean_vec, stds, sensor_outputs_copy)
                likelihood = np.prod(array_of_pdfs)
                high_likelihood = np.power(norm(0, 1).pdf(1.5), len(unused_subset_sensors))
                q_relative[trans.x][trans.y] = likelihood/high_likelihood
                power_grid[trans.x][trans.y] = delta_p
        q_max = np.max(q_relative)
        while q_max > 1:
            hypo = np.argmax(q_relative)
            x = hypo//self.grid_len
            y = hypo%self.grid_len
            print('**Intruder!**', (x, y), '; Q relative =', q_max)
            identified.append((x, y))
            pred_power.append(power_grid[x][y])
            self.delete_transmitter((x, y), power_grid[x][y], range(len(self.sensors)), sensor_outputs)
            subset_sensors = self.sensors_collect[self.key.format(hypo, R)]
            for sen in subset_sensors:
                self.sensors_used[sen] = True
            x_low  = max(0, x-R)
            x_high = min(self.grid_len, x+R)
            y_low  = max(0, y-R)
            y_high = min(self.grid_len, y+R)
            for i in range(x_low, x_high):
                for j in range(y_low, y_high):
                    q_relative[i][j] = 0
            q_max = np.max(q_relative)

        for trans in identified:
            pass
        return identified, pred_power



    #@profile
    def procedure2(self, sensor_outputs, intruders, fig, R, previous_identified):
        '''Our hypothesis-based localization algorithm's procedure 2
        Args:
            sensor_outputs (np.array)
            intruders (list): for plotting
            fig (int)       : for plotting
            R (int)
            previous_identified (list): list<(a, b)>
        Return:
            (list, list)
        '''
        if self.debug:
            visualize_sensor_output(self.grid_len, intruders, sensor_outputs, self.sensors, -80, fig)
        detected, power = [], []
        center_list = []
        center = self.get_center_sensor(sensor_outputs, R, center_list, previous_identified)
        combination_checked = {0}
        while center != -1:
            center_list.append(center)
            location = self.sensors[center].x*self.grid_len + self.sensors[center].y
            sensor_subset = self.sensors_collect[self.key.format(location, R)]
            self.ignore_screwed_sensor(sensor_subset, previous_identified, min_dist=2)
            hypotheses = [h for h in range(len(self.transmitters)) \
                          if math.sqrt((self.transmitters[h].x - self.sensors[center].x)**2 + (self.transmitters[h].y - self.sensors[center].y)**2) < R ]
            for t in range(2, 4):
                self.counter.time3_start()
                self.counter.time4_start()
                hypotheses_combination = list(combinations(hypotheses, t))
                hypotheses_combination = [x for x in hypotheses_combination if x not in combination_checked] # prevent checking the same combination again
                if len(hypotheses_combination) == 0:
                    break
                q_threshold = np.power(norm(0, 1).pdf(2), len(sensor_subset)) * (1./len(hypotheses_combination))
                combination_checked = combination_checked.union(set(hypotheses_combination))     # union of all combinations checked
                print('q-threshold = {}, inside = {}'.format(q_threshold, len(sensor_subset)))
                posterior, Q = self.procedure2_iteration(hypotheses_combination, sensor_outputs, sensor_subset)
                print('combination = {}; max Q = {}; posterior = {}'.format([ (hypo//self.grid_len, hypo%self.grid_len) for hypo in \
                       hypotheses_combination[np.argmax(Q)] ], np.max(Q), np.max(posterior)))
                if np.max(Q) > q_threshold and np.max(posterior) > 0.1:  # 
                    print('** Intruder! **')
                    hypo_comb = hypotheses_combination[np.argmax(Q)]
                    for hypo in hypo_comb:
                        x = hypo//self.grid_len
                        y = hypo%self.grid_len
                        detected.append((x, y))
                        power.append(0)
                        self.delete_transmitter((x, y), 0, range(len(self.sensors)), sensor_outputs)
                    if t == 2:
                        self.counter.proc_2_2 += 1
                    elif t == 3:
                        self.counter.proc_2_3 += 1
                    if self.debug:
                        visualize_sensor_output(self.grid_len, intruders, sensor_outputs, self.sensors, -80, fig)
                    break
                if t == 2:
                    self.counter.time3_end()
                elif t == 3:
                    self.counter.time4_end()
            center = self.get_center_sensor(sensor_outputs, R, center_list, previous_identified)
            center_list.append(center)
        return detected, power


    #@profile
    def procedure2_iteration(self, hypotheses_combination, sensor_outputs, sensor_subset):
        '''MAP over combinaton of hypotheses
        Args:
            hypotheses_combination (list): an element is a tuple of transmitter index, i.e. (t1, t2)
            sensor_outputs (np.array)
        Return:
            posterior (np.array): 1D array of posterior
            H_0 (bool): whether H_0 is the largest likelihood or not
            q (np.array): 2D array of Q
            power_grid (np.array): 2D array of power
        '''
        # Note try single power first
        posterior = np.zeros(len(hypotheses_combination))
        prior = 1./len(hypotheses_combination)
        for i in range(len(hypotheses_combination)):
            combination = hypotheses_combination[i]
            #if combination == (7*50+34, 9*50+32):
            #    print(combination)
            mean_vec = np.zeros(len(sensor_subset))
            for hypo in combination:
                mean_vec += db_2_power_(self.means[hypo][sensor_subset])
            mean_vec = power_2_db_(mean_vec)
            stds = np.sqrt(np.diagonal(self.covariance)[sensor_subset])
            array_of_pdfs = self.get_pdfs(mean_vec, stds, sensor_outputs[sensor_subset])
            likelihood = np.prod(array_of_pdfs)
            posterior[i] = likelihood * prior
        return posterior/np.sum(posterior), posterior  # question: is the denometer the summation of everything?


    def get_pdfs(self, mean_vec, stds, sensor_outputs):
        ''' Replace:
            norm(mean_vec, stds).pdf(sensor_outputs[sensor_subset])
            from 0.7 ms down to 0.013 ms
        '''
        sensor_outputs = np.abs((sensor_outputs - mean_vec) / stds)
        index = [i if i<390000 else 389999 for i in np.array(sensor_outputs * 10000, np.int)]
        return self.lookup_table_norm[index]


    def get_center_sensor(self, sensor_outputs, R, center_list, previous_identified):
        '''Check a center for procedure 2, if no centers, return -1
           Return the index of sensor with highest residual received power
        Args:
            sensor_outputs (np.array)
            R (int)
            center_list (list): a sensor cannot be center twice
            previous_identified (list): list<(a, b)>, previous identified transmitters
        Return:
            (int)
        '''
        sensor_outputs = np.copy(sensor_outputs)
        flag = True
        while flag:
            sen_descent = np.flip(np.argsort(sensor_outputs))
            for c in sen_descent:
                if c not in center_list:
                    center = c                       # the first sensor that hasn't been a center before
                    break
            if sensor_outputs[center] < -65:         # center's RSS has to > -65
                center = -1
                flag = False
                break
            center_sensor = self.sensors[center]
            close_to_transmitter = False             # a center sensor cannot be close to a detected transmitter -- save time
            for trans in previous_identified:
                if math.sqrt((center_sensor.x - trans[0])**2 + (center_sensor.y - trans[1])**2) <= 2:
                    close_to_transmitter = True
                    print((center_sensor.x, center_sensor.y), 'is close to transmitter', trans)
                    break
            if close_to_transmitter == True:
                sensor_outputs[center] = -80
                continue
            counter = 1
            for sen_index in range(len(self.sensors)):
                if sensor_outputs[sen_index] > -75:  # inaccurate residual power during deleting intruders
                    dist = math.sqrt((self.sensors[sen_index].x - center_sensor.x)**2 + (self.sensors[sen_index].y - center_sensor.y)**2)
                    if dist >=1 and dist < R:
                        counter += 1
                    if counter == 3:                 # need three "strong" sensor
                        flag = False
                        print('\nCenter =', (self.sensors[center].x, self.sensors[center].y), 'RSS =', sensor_outputs[center])
                        break
            else:
                sensor_outputs[center] = -80         # bug here ... shouldn't change the origin sensor output, fixed in first line
        return center


    #@profile
    def procedure1(self, hypotheses, sensor_outputs, intruders, fig, radius, previous_identified):
        '''Our hypothesis-based localization algorithm's procedure 1
        Args:
            hypotheses (list): transmitters (1D index) that has 2 or more sensors in radius with RSS > -80 
            sensor_outputs (np.array)
            intruders (list): for plotting
            fig (int):        for plotting
            radius (int) 
            previous_identified (list): an element is a tuple of 2D index, identified intruder in previous
        Return:
            (list, list): list<(a, b)>, list<float>
        '''
        num_cells = self.grid_len * self.grid_len + 1
        self.grid_priori = np.full(num_cells, 1.0 / (3.14*radius**2))  # modify priori to whatever himanshu likes
        #for intruder in previous_identified:
        #    self.grid_priori[intruder[0]*self.grid_len + intruder[1]] = 0 # set prior of previous identified location to zero
        self.ignore_boarders(edge=2)
        identified = []
        pred_power = []
        detected = True
        print('R = {}'.format(radius))
        offset = 0 #0.74 for synthetic, 0.5 for splat
        while detected:
            if self.debug:
                visualize_sensor_output(self.grid_len, intruders, sensor_outputs, self.sensors, -80, fig)
            detected = False
            previous_identified = list(set(previous_identified).union(set(identified)))
            posterior, H_0, Q, power = self.posterior_iteration(hypotheses, radius, sensor_outputs, fig, previous_identified)

            if H_0:
                print('H_0 is most likely')
                continue

            posterior = np.reshape(posterior[:-1], (self.grid_len, self.grid_len))
            if self.debug:
                visualize_q_prime(posterior, fig)
            indices = peak_local_max(posterior, 2, threshold_abs=0.8, exclude_border = False)

            if len(indices) == 0:
                print("No Q' peaks...")
                continue

            for index in indices:  # 2D index
                print('detected peak =', index, "; Q' =", round(posterior[index[0]][index[1]], 3), end='; ')
                q = Q[index[0]*self.grid_len + index[1]]
                location = index[0]*self.grid_len + index[1]
                subset_sensors = self.sensors_collect[self.key.format(location, radius)]
                self.ignore_screwed_sensor(subset_sensors, previous_identified, min_dist=2)
                sen_inside = len(subset_sensors)
                q_threshold = self.get_q_threshold_custom(sen_inside, radius)
                print('Q =', q, end='; ')
                print('q-threshold = {}, inside = {}'.format(q_threshold, sen_inside), end=' ')
                if q > q_threshold:
                    print(' **Intruder!**')
                    detected = True
                    p = power[index[0]][index[1]] - offset
                    self.delete_transmitter(index, p, range(len(self.sensors)), sensor_outputs)
                    for sen in subset_sensors:
                        self.sensors_used[sen] = True
                    identified.append(tuple(index))
                    pred_power.append(p)
                else:
                    print()
            print('---')
        return identified, pred_power


    def get_confined_area(self, sensor, R):
        '''Get the confined area described in MobiCom'17
        Args:
            sensor (Sensor)
            R (int)
        Return:
            list<(int, int)>: a list of 2D index
        '''
        confined_area = []
        min_x = sensor.x - R
        min_y = sensor.y - R
        for x in range(min_x, min_x + 2*R):
            if x < 0 or x >= self.grid_len:
                continue
            for y in range(min_y, min_y + 2*R):
                if y < 0 or y >= self.grid_len:
                    continue
                dist = math.sqrt((x - sensor.x)**2 + (y - sensor.y)**2)
                if dist < R:
                    confined_area.append((x, y))
        return confined_area


    def euclidean(self, location1, location2, minPL):
        '''
        Args:
            location1 (tuple) (x, y)
            location2 (tuple) (x, y)
            minPL     (float) minimum path length
        Return:
            float
        '''
        dist = np.sqrt( (location1[0] - location2[0])**2 + (location1[1] - location2[1])**2 )
        dist *= 1             #SPLAT data = 25; IPSN data = 1; representing the size of grid
        dist = minPL if dist < minPL else dist
        return dist


    def compute_path_loss(self, sensor_outputs):
        distance_vector = np.zeros(2000)
        path_loss_vector = np.zeros(2000)
        i = 0
        for trans in self.transmitters:
            for sensor in self.sensors:
                path_loss_vector[i] = 30 - sensor_outputs[sensor.index]
                distance_vector[i] = self.euclidean((sensor.x, sensor.y), (trans.x, trans.y), minPL=1)
                i += 1
                if i == 2000:
                    break
            if i == 2000:
                break
        #print(distance_vector)
        distance_vector = np.log(distance_vector)
        #power_vector = db_2_amplitude(power_vector)
        #print(np.isnan(power_vector).any())
        #print(np.isinf(power_vector).any())
        A = np.vstack([distance_vector, np.ones(len(path_loss_vector))]).T
        #print(A.shape, power_vector.shape)
        #print('Dist = ', distance_vector)
        W, n = nnls(A, path_loss_vector)[0]
        #print('Power = ', path_loss_vector, 'W = ', W, 'n = ', n)

        # import matplotlib.pyplot as plt
        # plt.plot(distance_vector, path_loss_vector, 'o', label='Original data', markersize=10)
        # plt.plot(distance_vector, W * distance_vector + n, 'r', label='Fitted line')
        #
        # plt.legend()
        # plt.show()

        return (W, n)


    def splot_localization(self, sensor_outputs, intruders, fig):
        '''The precise implemenation of SPLOT from MobiCom'17
        Args:
            sensor_outputs (np.array<float>): the RSS of sensors
            intruders (list<Transmitter>):    for visualization only
            fig (int):                        for visualization only
        Return:
            list<(int, int)>: a list of localized locations, each location is (a, b)
        '''
        sigma_x_square = 0.5
        delta_c        = 1
        n_p            = 2     # 2.46
        minPL          = 0.5   # For SPLOT 1.5, for Ridge and LASSO 1.0
        delta_N_square = 1     # no specification in MobiCom'17 ?
        R1             = 8
        R2             = 8     # larger R might help for ridge regression
        threshold      = -70

        if self.debug:
            visualize_sensor_output(self.grid_len, intruders, sensor_outputs, self.sensors, -80, fig)

        weight_global  = np.zeros((self.grid_len, self.grid_len))
        sensor_sorted_index = np.flip(np.argsort(sensor_outputs))

        if threshold is None:
            threshold = int(0.3*len(sensor_outputs))       # threshold: instead of a specific value, it is a percentage of sensors
            sensor_sorted_index = np.flip(np.argsort(sensor_outputs))  # decrease
            threshold = sensor_outputs[sensor_sorted_index[threshold]]
            threshold = threshold if threshold > -75 else -75
        #gradient, noise = self.compute_path_loss(sensor_outputs)
        detected_intruders = []
        sensor_outputs_copy = np.copy(sensor_outputs)
        local_maximum_list = []

        for i in range(len(sensor_outputs_copy)): # Obtain local maximum within radius size_R
            current_sensor = self.sensors[sensor_sorted_index[i]]
            current_sensor_output = sensor_outputs_copy[current_sensor.index]
            if current_sensor_output < threshold:
                continue
            sensor_subset = self.collect_sensors_in_radius(R1, current_sensor)
            local_maximum_list.append(current_sensor.index)
            for sen_num in sensor_subset:
                sensor_outputs_copy[sen_num] = -85

        #Obtained local maximum list; now compute intruder location
        detected_intruders = []
        for sen_local_max in local_maximum_list:
            sensor_subset = self.collect_sensors_in_radius(R2, self.sensors[sen_local_max])
            confined_area = self.get_confined_area(self.sensors[sen_local_max], R2)
            total_voxel = len(confined_area)   # the Q in the paper
            W_matrix = np.zeros((len(sensor_subset), total_voxel))
            for i, sen_index in enumerate(sensor_subset):
                sensor = self.sensors[sen_index]
                for q, voxel in enumerate(confined_area):
                    dist = self.euclidean((sensor.x, sensor.y), voxel, minPL)
                    W_matrix[i, q] = (dist/0.5) ** (-n_p)

            
            W_transpose = np.transpose(W_matrix)
            y = np.zeros(len(sensor_subset))
            for i in range(len(sensor_subset)):
                y[i] = db_2_power(sensor_outputs[sensor_subset[i]])
            #'''
            Cx = np.zeros((total_voxel, total_voxel))
            for j in range(total_voxel):
                voxel_j = confined_area[j]
                for l in range(total_voxel):
                    voxel_l = confined_area[l]
                    dist = self.euclidean(voxel_j, voxel_l, minPL)
                    Cx[j][l] = sigma_x_square * np.power(np.e, -dist/delta_c)
            Cx = delta_N_square * Cx

            try:
                X1 = np.matmul(W_transpose, W_matrix)
                X1 = X1 + Cx
                X2 = np.linalg.inv(X1)
            except Exception as e:
                print(e)
            X = np.matmul(X2, W_transpose)
            X = np.matmul(X, y)               # shape of X: (total_voxel, 1)
            weight_local = np.zeros((self.grid_len, self.grid_len))
            for i, x in enumerate(X):
                voxel = confined_area[i]
                weight_local[voxel[0]][voxel[1]] = x
                weight_global[voxel[0]][voxel[1]] = x
            
            if self.debug:
                visualize_splot(weight_local, 'splot', str(fig)+'-'+str(self.sensors[sen_local_max].x)+'-'+str(self.sensors[sen_local_max].y))

            index = np.argmax(X)
            detected_intruders.append(confined_area[index])
            #'''

            '''
            from sklearn.linear_model import Lasso, Ridge
            linear = Ridge(alpha=0.1)
            #linear = Lasso(alpha=0.0000001)   # 0.00001 for synthetic
            linear.fit(W_matrix, y)
            X = linear.coef_
            weight_local = np.zeros((self.grid_len, self.grid_len))
            for i, x in enumerate(X):
                voxel = confined_area[i]
                weight_local[voxel[0]][voxel[1]] = x
                weight_global[voxel[0]][voxel[1]] = x
            if self.debug:
                visualize_splot(weight_local, 'splot-ridge', str(fig)+'-'+str(self.sensors[sen_local_max].x)+'-'+str(self.sensors[sen_local_max].y))
            index = np.argmax(X)
            detected_intruders.append(confined_area[index])
            '''
        if self.debug:
            visualize_splot(weight_global, 'splot-ridge', fig)

        return detected_intruders


    def convert_to_pos(self, true_indices):
        list = []
        for index in true_indices:
            x = index // self.grid_len
            y = index % self.grid_len
            list.append((x, y))
        return list



def main1():
    '''main 1: synthetic data + SPLOT
    '''
    selectsensor = Localization(grid_len=50)
    selectsensor.init_data('data50/homogeneous-200/cov', 'data50/homogeneous-200/sensors', 'data50/homogeneous-200/hypothesis')
    true_powers = [-2, -1, 0, 1, 2]
    #true_powers = [0, 0, 0, 0, 0]   # no varing power
    selectsensor.vary_power(true_powers)

    repeat = 5
    errors = []
    misses = []
    false_alarms = []
    start = time.time()
    for i in range(0, repeat):
        print('\n\nTest ', i)
        random.seed(i)
        np.random.seed(i)
        true_indices, true_powers = generate_intruders(grid_len=selectsensor.grid_len, edge=2, num=5, min_dist=20, powers=true_powers)
        #true_indices, true_powers = generate_intruders_2(grid_len=selectsensor.grid_len, edge=2, min_dist=16, max_dist=5, intruders=true_indices, powers=true_powers, cluster_size=3)
        #true_indices = [x * selectsensor.grid_len + y for (x, y) in true_indices]
        intruders, sensor_outputs = selectsensor.set_intruders(true_indices=true_indices, powers=true_powers, randomness=True)

        pred_locations = selectsensor.splot_localization(sensor_outputs, intruders, fig=i)

        true_locations = selectsensor.convert_to_pos(true_indices)
        try:
            error, miss, false_alarm = selectsensor.compute_error2(true_locations, pred_locations)
            if len(error) != 0:
                errors.extend(error)
            misses.append(miss)
            false_alarms.append(false_alarm)
            print('error/miss/false/power = {}/{}/{}'.format(np.array(error).mean(), miss, false_alarm) )
            visualize_localization(selectsensor.grid_len, true_locations, pred_locations, i)
        except Exception as e:
            print(e)

    try:
        errors = np.array(errors)
        print('(mean/max/min) error=({}/{}/{}), miss=({}/{}/{}), false_alarm=({}/{}/{})'.format(round(errors.mean(), 3), round(errors.max(), 3), round(errors.min(), 3), \
              round(sum(misses)/repeat, 3), max(misses), min(misses), round(sum(false_alarms)/repeat, 3), max(false_alarms), min(false_alarms)) )
        print('Ours! time = ', time.time()-start)
    except:
        print('Empty list!')


def main2():
    '''main 2: synthetic data + Our localization
    '''
    ll = Localization(grid_len=50, debug=True)
    ll.init_data('data50/homogeneous-200/cov', 'data50/homogeneous-200/sensors', 'data50/homogeneous-200/hypothesis')
    num_of_intruders = 10

    a, b = 0, 2
    errors = []
    misses = []
    false_alarms = []
    power_errors = []
    ll.counter.num_exper = b-a
    ll.counter.time_start()
    #for i in [2, 6, 10, 12, 13, 19]:
    for i in range(a, b):
        print('\n\nTest ', i)
        random.seed(i)
        true_powers = [random.uniform(-2, 2) for i in range(num_of_intruders)]
        random.seed(i)
        np.random.seed(i)
        true_indices, true_powers = generate_intruders(grid_len=ll.grid_len, edge=2, num=num_of_intruders, min_dist=1, powers=true_powers)
        #true_indices, true_powers = generate_intruders_2(grid_len=selectsensor.grid_len, edge=2, min_dist=16, max_dist=5, intruders=true_indices, powers=true_powers, cluster_size=3)
        #true_indices = [x * selectsensor.grid_len + y for (x, y) in true_indices]

        intruders, sensor_outputs = ll.set_intruders(true_indices=true_indices, powers=true_powers, randomness=True)

        pred_locations, pred_power = ll.our_localization(sensor_outputs, intruders, i)
        true_locations = ll.convert_to_pos(true_indices)

        try:
            error, miss, false_alarm, power_error = ll.compute_error(true_locations, true_powers, pred_locations, pred_power)
            if len(error) != 0:
                errors.extend(error)
                power_errors.extend(power_error)
            misses.append(miss)
            false_alarms.append(false_alarm)
            print('error/miss/false/power = {:.3f}/{}/{}/{:.3f}'.format(np.array(error).mean(), miss, false_alarm, np.array(power_error).mean()) )
            if ll.debug:
                visualize_localization(ll.grid_len, true_locations, pred_locations, i)
        except Exception as e:
            print(e)

    try:
        errors = np.array(errors)
        power_errors = np.array(power_errors)
        print('(mean/max/min) error=({:.3f}/{:.3f}/{:.3f}), miss=({:.3f}/{}/{}), false_alarm=({:.3f}/{}/{}), power=({:.3f}/{:.3f}/{:.3f})'.format(errors.mean(), errors.max(), errors.min(), \
              sum(misses)/(b-a), max(misses), min(misses), sum(false_alarms)/(b-a), max(false_alarms), min(false_alarms), power_errors.mean(), power_errors.max(), power_errors.min() ) )
        ll.counter.time_end()
        ratios = ll.counter.procedure_ratios()
        print(ratios)
        print('Proc-1 time = {:.3f}, Proc-1.1 = {:.3f}， Proc-2-2 time = {:.3f}, Proc-2-3 time = {:.3f}'.format(ll.counter.time1_average(), ll.counter.time2_average(), ll.counter.time3_average(), ll.counter.time4_average()))
    except Exception as e:
        print(e)


def main4():
    '''main 4: SPLAT data + Our localization
    '''
    selectsensor = Localization(grid_len=40)
    #selectsensor.init_data('dataSplat/homogeneous-100/cov', 'dataSplat/homogeneous-100/sensors', 'dataSplat/homogeneous-100/hypothesis')
    #selectsensor.init_data('dataSplat/homogeneous-150/cov', 'dataSplat/homogeneous-150/sensors', 'dataSplat/homogeneous-150/hypothesis')
    selectsensor.init_data('dataSplat/homogeneous-200/cov', 'dataSplat/homogeneous-200/sensors', 'dataSplat/homogeneous-200/hypothesis')
    #selectsensor.init_data('dataSplat/homogeneous-250/cov', 'dataSplat/homogeneous-250/sensors', 'dataSplat/homogeneous-250/hypothesis')
    #selectsensor.init_data('dataSplat/homogeneous-300/cov', 'dataSplat/homogeneous-300/sensors', 'dataSplat/homogeneous-300/hypothesis')

    num_of_intruders = 1
    repeat = 1
    errors = []
    misses = []
    false_alarms = []
    power_errors = []
    start = time.time()
    for i in range(0, repeat):
        print('\n\nTest ', i)
        random.seed(i)
        true_powers = [random.uniform(-2, 2) for i in range(num_of_intruders)]
        random.seed(i)
        np.random.seed(i)
        true_indices, true_powers = generate_intruders(grid_len=selectsensor.grid_len, edge=2, num=num_of_intruders, min_dist=1, powers=true_powers)
        #true_indices, true_powers = generate_intruders_2(grid_len=selectsensor.grid_len, edge=2, min_dist=16, max_dist=5, intruders=true_indices, powers=true_powers, cluster_size=3)
        #true_indices = [x * selectsensor.grid_len + y for (x, y) in true_indices]

        intruders, sensor_outputs = selectsensor.set_intruders(true_indices=true_indices, powers=true_powers, randomness=True)

        pred_locations, pred_power = selectsensor.our_localization(sensor_outputs, intruders, i)
        true_locations = selectsensor.convert_to_pos(true_indices)

        try:
            error, miss, false_alarm, power_error = selectsensor.compute_error(true_locations, true_powers, pred_locations, pred_power)
            if len(error) != 0:
                errors.extend(error)
                power_errors.extend(power_error)
            misses.append(miss)
            false_alarms.append(false_alarm)
            print('error/miss/false/power = {}/{}/{}/{}'.format(np.array(error).mean(), miss, false_alarm, np.array(power_error).mean()) )
            visualize_localization(selectsensor.grid_len, true_locations, pred_locations, i)
        except Exception as e:
            print(e)

    try:
        print('(mean/max/min) error=({}/{}/{}), miss=({}/{}/{}), false_alarm=({}/{}/{}), power=({}/{}/{})'.format(round(sum(errors)/len(errors), 3), round(max(errors), 3), round(min(errors), 3), \
              round(sum(misses)/repeat, 3), max(misses), min(misses), round(sum(false_alarms)/repeat, 3), max(false_alarms), min(false_alarms), round(sum(power_errors)/len(power_errors), 3), round(max(power_errors), 3), round(min(power_errors), 3) ) )
        print('Ours! time = ', round(time.time()-start, 3))
    except Exception as e:
        print(e)


def main5():
    '''main 5: SPLAT data + SPLOT localization
    '''
    selectsensor = Localization(grid_len=40)
    #selectsensor.init_data('dataSplat/homogeneous-100/cov', 'dataSplat/homogeneous-100/sensors', 'dataSplat/homogeneous-100/hypothesis')
    #selectsensor.init_data('dataSplat/homogeneous-150/cov', 'dataSplat/homogeneous-150/sensors', 'dataSplat/homogeneous-150/hypothesis')
    #selectsensor.init_data('dataSplat/homogeneous-200/cov', 'dataSplat/homogeneous-200/sensors', 'dataSplat/homogeneous-200/hypothesis')
    #selectsensor.init_data('dataSplat/homogeneous-250/cov', 'dataSplat/homogeneous-250/sensors', 'dataSplat/homogeneous-250/hypothesis')
    selectsensor.init_data('dataSplat/homogeneous-300/cov', 'dataSplat/homogeneous-300/sensors', 'dataSplat/homogeneous-300/hypothesis')
    true_powers = [-2, -1, 0, 1, 2]                                                    # 5 intruders
    selectsensor.vary_power(true_powers)

    repeat = 30
    errors = []
    misses = []
    false_alarms = []
    start = time.time()
    for i in range(0, repeat):
        print('\n\nTest ', i)
        random.seed(i)
        np.random.seed(i)
        true_indices, true_powers = generate_intruders(grid_len=selectsensor.grid_len, edge=2, num=5, min_dist=1, powers=true_powers)
        #true_indices, true_powers = generate_intruders_2(grid_len=selectsensor.grid_len, edge=2, min_dist=16, max_dist=5, intruders=true_indices, powers=true_powers, cluster_size=3)
        #true_indices = [x * selectsensor.grid_len + y for (x, y) in true_indices]

        intruders, sensor_outputs = selectsensor.set_intruders(true_indices=true_indices, powers=true_powers, randomness=False)

        pred_locations = selectsensor.splot_localization(sensor_outputs, intruders, fig=i)
        true_locations = selectsensor.convert_to_pos(true_indices)

        try:
            error, miss, false_alarm = selectsensor.compute_error2(true_locations, pred_locations)
            if error >= 0:
                errors.append(error)
            misses.append(miss)
            false_alarms.append(false_alarm)
            print('error/miss/false/power = {}/{}/{}'.format(error, miss, false_alarm) )
            visualize_localization(selectsensor.grid_len, true_locations, pred_locations, i)
        except Exception as e:
            print(e)

    try:
        print('(mean/max/min) error=({}/{}/{}), miss=({}/{}/{}), false_alarm=({}/{}/{})'.format(round(sum(errors)/len(errors), 3), round(max(errors), 3), round(min(errors), 3), \
              round(sum(misses)/repeat, 3), max(misses), min(misses), round(sum(false_alarms)/repeat, 3), max(false_alarms), min(false_alarms)) )
        print('Ours! time = ', time.time()-start)
    except:
        print('Empty list!')


def main6():
    ''' Clusting
    '''
    # SelectSensor version on May 31, 2019
    selectsensor = Localization(grid_len=50)
    selectsensor.init_data('data50/homogeneous-200/cov', 'data50/homogeneous-200/sensors', 'data50/homogeneous-200/hypothesis')

    num_of_intruders = 5

    a, b = 0, 2
    errors = []
    misses = []
    false_alarms = []
    power_errors = []
    start = time.time()
    for i in range(a, b):
        print('\n\nTest ', i)
        random.seed(i)
        true_powers = [random.uniform(-2, 2) for i in range(num_of_intruders)]
        random.seed(i)
        np.random.seed(i)
        true_indices, true_powers = generate_intruders(grid_len=selectsensor.grid_len, edge=2, num=num_of_intruders, min_dist=1, powers=true_powers)
        intruders, sensor_outputs = selectsensor.set_intruders(true_indices=true_indices, powers=true_powers, randomness=True)

        pred_locations = selectsensor.cluster_localization(intruders, sensor_outputs, num_of_intruders)

        true_locations = selectsensor.convert_to_pos(true_indices)

        try:
            error, miss, false_alarm = selectsensor.compute_error2(true_locations, pred_locations)
            if len(error) != 0:
                errors.extend(error)
            misses.append(miss)
            false_alarms.append(false_alarm)
            print('error/miss/false/power = {}/{}/{}'.format(np.array(error).mean(), miss, false_alarm) )
        except Exception as e:
            print(e)

    try:
        errors = np.array(errors)
        power_errors = np.array(power_errors)
        #np.savetxt('{}-cluster-error.txt'.format(num_of_intruders), errors, delimiter=',')
        #np.savetxt('{}-cluster-miss.txt'.format(num_of_intruders), misses, delimiter=',')
        #np.savetxt('{}-cluster-false.txt'.format(num_of_intruders), false_alarms, delimiter=',')
        #np.savetxt('{}-cluster-time.txt'.format(num_of_intruders), [(time.time()-start)/(b-a)], delimiter=',')
        print('(mean/max/min) error=({}/{}/{}), miss=({}/{}/{}), false_alarm=({}/{}/{})'.format(round(errors.mean(), 3), round(errors.max(), 3), round(errors.min(), 3), \
              round(sum(misses)/(b-a), 3), max(misses), min(misses), round(sum(false_alarms)/(b-a), 3), max(false_alarms), min(false_alarms) ) )
        print('Ours! time = ', round(time.time()-start, 3))
    except Exception as e:
        print(e)


if __name__ == '__main__':
    main1()
    #main2()
    #main3()
    #main4()
    #main5()
    #main6()