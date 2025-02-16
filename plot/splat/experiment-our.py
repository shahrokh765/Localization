import numpy as np
import pandas as pd
import sys
import time
import random
sys.path.append('../..')
from localize import Localization
from utility import generate_intruders


# SelectSensor version on May 31, 2019
ll = Localization(grid_len=40)
ll.init_data('../../dataSplat/homogeneous-200/cov', '../../dataSplat/homogeneous-200/sensors', '../../dataSplat/homogeneous-200/hypothesis')

num_of_intruders = 3

a, b = 0, 50
errors = []
misses = []
false_alarms = []
power_errors = []
ll.counter.num_exper = b-a
ll.counter.time_start()
for i in range(a, b):
    print('\n\nTest ', i)
    random.seed(i)
    true_powers = [random.uniform(-2, 2) for i in range(num_of_intruders)]
    random.seed(i)
    np.random.seed(i)
    true_indices, true_powers = generate_intruders(grid_len=ll.grid_len, edge=2, num=num_of_intruders, min_dist=1, powers=true_powers)

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
        print('error/miss/false/power = {}/{}/{}/{}'.format(np.array(error).mean(), miss, false_alarm, np.array(power_error).mean()) )
    except Exception as e:
        print(e)

try:
    ll.counter.time_end()
    errors = np.array(errors)
    power_errors = np.array(power_errors)
    np.savetxt('{}-ours-error.txt'.format(num_of_intruders), errors, delimiter=',')
    np.savetxt('{}-ours-miss.txt'.format(num_of_intruders), misses, delimiter=',')
    np.savetxt('{}-ours-false.txt'.format(num_of_intruders), false_alarms, delimiter=',')
    np.savetxt('{}-ours-power.txt'.format(num_of_intruders), power_errors, delimiter=',')
    np.savetxt('{}-ours-time.txt'.format(num_of_intruders), [ll.counter.time1_average(), ll.counter.time2_average(), ll.counter.time3_average(), ll.counter.time4_average()], delimiter=',')
    np.savetxt('{}-ours-ratio.txt'.format(num_of_intruders), ll.counter.procedure_ratios(), delimiter=',')
    print('(mean/max/min) error=({:.3f}/{:.3f}/{:.3f}), miss=({:.3f}/{}/{}), false_alarm=({:.3f}/{}/{}), power=({:.3f}/{:.3f}/{:.3f})'.format(errors.mean(), errors.max(), errors.min(), \
              sum(misses)/(b-a), max(misses), min(misses), sum(false_alarms)/(b-a), max(false_alarms), min(false_alarms), power_errors.mean(), power_errors.max(), power_errors.min() ) )
except Exception as e:
    print(e)