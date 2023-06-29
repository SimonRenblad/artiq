

class SimpleVCDParser:
    _VALUE_CHANGE = set(('0', '1', 'x', 'z', 'X', 'Z'))
    _VECTOR_VALUE_CHANGE = set(('b', 'B', 'r', 'R'))
    
    def __init__(self, filename):
        self.channels = []
        self.timescale_magnitude = 1
        self.timescale_factor = 1e-12
        self.unit = 'ps'
        self.codes = dict()
        self.data = dict()
        self.sizes = dict()
        with open(filename, 'r') as file:
            self._parse(file)
        
    def _parse(self, file):
        first_time = True
        time = 0
        for line in file:
            line = line.strip()
            if line[0] == '#':
                time = self._parse_timestamp(line, first_time)
                first_time = False
            elif line[0] in self._VALUE_CHANGE:
                self._parse_value_change(line, time)
            elif line[0] in self._VECTOR_VALUE_CHANGE:
                self._parse_vector_value_change(line, time)
            elif '$var' in line:
                self._parse_channel(line)
            elif '$timescale' in line:
                self._parse_timescale(line)
        self.end_time = time

    def _parse_timestamp(self, line, first_time):
        time = int(line.split()[0][1:])
        if first_time:
            self.start_time = time
        return time

    def _parse_value_change(self, line, time):
        value = line[0]
        code = line[1:]
        self._add_value_change(value, code, time)

    def _parse_vector_value_change(self, line, time):
        value, code = line[1:].split()
        self._add_value_change(value, code, time)

    def _add_value_change(self, value, code, time):
        if code in self.codes.keys():
            channel = self.codes[code]
            self.data[channel].append((value, time))

    def _parse_channel(self, line):
        l = line.split()
        channel_type = l[1]
        size = int(l[2])
        code = l[3]
        name = ''.join(l[4:-1])
        self.codes[code] = name
        self.sizes[name] = size
        self.data[name] = []
        self.channels.append(name)

    def _parse_timescale(self, line):
        rem = line.split()[1:-1]
        rem = ''.join(rem)
        mag = 1
        if not rem[0] == '1':
            return
        i = 1
        while rem[i] == '0':
           mag *= 10
           i += 1
        unit_mapping = {'s': (1e0, 's'),
                   'm': (1e-3, 'ms'),
                   'u': (1e-6, 'us'),
                   'n': (1e-9, 'ns'),
                   'p': (1e-12, 'ps'),
                   'f': (1e-15, 'fs')}
        fac, unit = unit_mapping[rem[i]]
        self.timescale_magnitude = mag
        self.timescale_factor = fac
        self.unit = unit
