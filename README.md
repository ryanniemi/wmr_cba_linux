# wmr_cba_linux

Linux tools for West Mountain Radio CBA battery analyzers like the [CBA IV](http://www.westmountainradio.com/cba.php).

These tools talk to the battery analyzer via libusb, using [da66en's python_wmr_cba library](https://github.com/da66en/python_wmr_cba/).

## Installation

On Debian and Ubuntu, install the python3-usb package:
```
sudo apt-get install python3-usb
```

## Usage

Run a battery test at 0.2A, with a cutoff voltage of 10.0VDC, showing stats
and writing a line to the battery.csv file every 10 seconds:
```
sudo ./cba_cli.py --amps 0.2 --cutoff 10.0 --interval 10 --csv battery.csv
```

## License

FIXME
