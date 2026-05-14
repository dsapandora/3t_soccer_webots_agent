// soccerv3_native.cpp
// pybind11 bindings exposing Soccer.cpp logic to Python.
// The C++ class Soccerv3Native owns the Webots Robot, the three RobotisOp2
// managers, and all device handles. Python sees only the high-level helpers
// it needs to mirror Soccer.cpp's run() loop.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <RobotisOp2GaitManager.hpp>
#include <RobotisOp2MotionManager.hpp>
#include <RobotisOp2VisionManager.hpp>
#include <webots/Accelerometer.hpp>
#include <webots/Camera.hpp>
#include <webots/Gyro.hpp>
#include <webots/LED.hpp>
#include <webots/Motor.hpp>
#include <webots/PositionSensor.hpp>
#include <webots/Robot.hpp>
#include <webots/Speaker.hpp>

#include <array>
#include <cstdlib>
#include <string>
#include <tuple>
#include <vector>

namespace py = pybind11;
using namespace webots;
using namespace managers;

#define NMOTORS 20

static const char *kMotorNames[NMOTORS] = {
  "ShoulderR", "ShoulderL", "ArmUpperR", "ArmUpperL", "ArmLowerR",
  "ArmLowerL", "PelvYR",    "PelvYL",    "PelvR",     "PelvL",
  "LegUpperR", "LegUpperL", "LegLowerR", "LegLowerL", "AnkleR",
  "AnkleL",    "FootR",     "FootL",     "Neck",      "Head"
};

class Soccerv3Native : public Robot {
public:
  Soccerv3Native() : Robot() {
    mTimeStep = static_cast<int>(getBasicTimeStep());

    mEyeLED = getLED("EyeLed");
    mHeadLED = getLED("HeadLed");
    if (mHeadLED) mHeadLED->set(0x00FF00);
    mBackLedRed = getLED("BackLedRed");
    mBackLedGreen = getLED("BackLedGreen");
    mBackLedBlue = getLED("BackLedBlue");

    mCamera = getCamera("Camera");
    if (mCamera) mCamera->enable(2 * mTimeStep);
    mAccelerometer = getAccelerometer("Accelerometer");
    if (mAccelerometer) mAccelerometer->enable(mTimeStep);
    mGyro = getGyro("Gyro");
    if (mGyro) mGyro->enable(mTimeStep);
    mSpeaker = getSpeaker("Speaker");

    for (int i = 0; i < NMOTORS; i++) {
      mMotors[i] = getMotor(kMotorNames[i]);
      std::string sensorName = std::string(kMotorNames[i]) + "S";
      mPositionSensors[i] = getPositionSensor(sensorName);
      if (mPositionSensors[i]) mPositionSensors[i]->enable(mTimeStep);
      mMinPos[i] = mMotors[i] ? mMotors[i]->getMinPosition() : -1.0;
      mMaxPos[i] = mMotors[i] ? mMotors[i]->getMaxPosition() : 1.0;
    }

    mMotionManager = new RobotisOp2MotionManager(this);
    mGaitManager = new RobotisOp2GaitManager(this, "config.ini");
    if (mCamera)
      mVisionManager = new RobotisOp2VisionManager(
        mCamera->getWidth(), mCamera->getHeight(), 28, 20, 50, 45, 0, 30);
    else
      mVisionManager = nullptr;
  }

  virtual ~Soccerv3Native() {
    delete mMotionManager;
    delete mGaitManager;
    delete mVisionManager;
  }

  int timeStep() const { return mTimeStep; }
  double currentTime() { return getTime(); }

  // Step the simulation; returns false when Webots is shutting down.
  bool myStep() {
    int ret = step(mTimeStep);
    return ret != -1;
  }

  void waitMs(int ms) {
    double startTime = getTime();
    double s = static_cast<double>(ms) / 1000.0;
    while (s + startTime >= getTime()) {
      if (!myStep()) std::exit(EXIT_SUCCESS);
    }
  }

  // ----- LEDs -----
  void setEyeLED(uint32_t color) { if (mEyeLED) mEyeLED->set(color); }
  void setHeadLED(uint32_t color) { if (mHeadLED) mHeadLED->set(color); }

  // ----- Speaker -----
  void speak(const std::string &text, double volume = 1.0) {
    if (mSpeaker) mSpeaker->speak(text, volume);
  }

  // ----- Accelerometer / Gyro -----
  std::array<double, 3> accelerometer() {
    if (!mAccelerometer) return {0.0, 0.0, 0.0};
    const double *v = mAccelerometer->getValues();
    return {v[0], v[1], v[2]};
  }

  std::array<double, 3> gyro() {
    if (!mGyro) return {0.0, 0.0, 0.0};
    const double *v = mGyro->getValues();
    return {v[0], v[1], v[2]};
  }

  // ----- Vision -----
  // Returns (found, x, y) with x,y normalized to [-1.0, 1.0].
  std::tuple<bool, double, double> getBallCenter() {
    if (!mCamera || !mVisionManager) return {false, 0.0, 0.0};
    const int width = mCamera->getWidth();
    const int height = mCamera->getHeight();
    const unsigned char *im = mCamera->getImage();
    if (!im) return {false, 0.0, 0.0};

    double x = 0.0, y = 0.0;
    bool found = mVisionManager->getBallCenter(x, y, im);
    if (!found) return {false, 0.0, 0.0};
    return {true, 2.0 * x / width - 1.0, 2.0 * y / height - 1.0};
  }

  // Raw camera frame as BGRA bytes (width * height * 4). Webots stores pixels
  // as B, G, R, A in that order. vision.py reshapes via numpy.frombuffer.
  py::bytes getCameraFrame() {
    if (!mCamera) return py::bytes("", 0);
    const unsigned char *im = mCamera->getImage();
    if (!im) return py::bytes("", 0);
    const size_t n = static_cast<size_t>(mCamera->getWidth()) *
                     static_cast<size_t>(mCamera->getHeight()) * 4;
    return py::bytes(reinterpret_cast<const char *>(im), n);
  }

  int cameraWidth() const { return mCamera ? mCamera->getWidth() : 0; }
  int cameraHeight() const { return mCamera ? mCamera->getHeight() : 0; }

  // ----- Motors / motion / gait -----
  void setMotorPosition(int idx, double position) {
    if (idx < 0 || idx >= NMOTORS || !mMotors[idx]) return;
    mMotors[idx]->setPosition(position);
  }

  double minMotorPosition(int idx) const {
    if (idx < 0 || idx >= NMOTORS) return -1.0;
    return mMinPos[idx];
  }

  double maxMotorPosition(int idx) const {
    if (idx < 0 || idx >= NMOTORS) return 1.0;
    return mMaxPos[idx];
  }

  // Motion playback (by page id).
  void playMotion(int pageId, bool sync = true) {
    if (mMotionManager) mMotionManager->playPage(pageId, sync);
  }

  // Gait control.
  void gaitStart() { if (mGaitManager) mGaitManager->start(); }
  void gaitStop() { if (mGaitManager) mGaitManager->stop(); }
  void gaitStep() { if (mGaitManager) mGaitManager->step(mTimeStep); }
  void setXAmplitude(double v) { if (mGaitManager) mGaitManager->setXAmplitude(v); }
  void setYAmplitude(double v) { if (mGaitManager) mGaitManager->setYAmplitude(v); }
  void setAAmplitude(double v) { if (mGaitManager) mGaitManager->setAAmplitude(v); }
  void setBalanceEnable(bool v) { if (mGaitManager) mGaitManager->setBalanceEnable(v); }

private:
  int mTimeStep;
  Motor *mMotors[NMOTORS] = {};
  PositionSensor *mPositionSensors[NMOTORS] = {};
  double mMinPos[NMOTORS] = {};
  double mMaxPos[NMOTORS] = {};

  LED *mEyeLED = nullptr;
  LED *mHeadLED = nullptr;
  LED *mBackLedRed = nullptr;
  LED *mBackLedGreen = nullptr;
  LED *mBackLedBlue = nullptr;
  Camera *mCamera = nullptr;
  Accelerometer *mAccelerometer = nullptr;
  Gyro *mGyro = nullptr;
  Speaker *mSpeaker = nullptr;

  RobotisOp2MotionManager *mMotionManager = nullptr;
  RobotisOp2GaitManager *mGaitManager = nullptr;
  RobotisOp2VisionManager *mVisionManager = nullptr;
};

PYBIND11_MODULE(soccerv3_native, m) {
  m.doc() = "pybind11 wrapper exposing Webots ROBOTIS-OP2 managers to Python";

  py::class_<Soccerv3Native>(m, "Soccerv3Native")
    .def(py::init<>())
    .def("time_step", &Soccerv3Native::timeStep)
    .def("current_time", &Soccerv3Native::currentTime)
    .def("step", &Soccerv3Native::myStep,
         "Advance one Webots step. Returns False when the simulator is stopping.")
    .def("wait_ms", &Soccerv3Native::waitMs)
    .def("set_eye_led", &Soccerv3Native::setEyeLED)
    .def("set_head_led", &Soccerv3Native::setHeadLED)
    .def("speak", &Soccerv3Native::speak,
         py::arg("text"), py::arg("volume") = 1.0)
    .def("accelerometer", &Soccerv3Native::accelerometer)
    .def("gyro", &Soccerv3Native::gyro)
    .def("get_ball_center", &Soccerv3Native::getBallCenter,
         "Returns (found, x, y) with x,y in [-1.0, 1.0].")
    .def("get_camera_frame", &Soccerv3Native::getCameraFrame,
         "Raw BGRA camera buffer (width * height * 4 bytes).")
    .def("camera_width", &Soccerv3Native::cameraWidth)
    .def("camera_height", &Soccerv3Native::cameraHeight)
    .def("set_motor_position", &Soccerv3Native::setMotorPosition)
    .def("min_motor_position", &Soccerv3Native::minMotorPosition)
    .def("max_motor_position", &Soccerv3Native::maxMotorPosition)
    .def("play_motion", &Soccerv3Native::playMotion,
         py::arg("page_id"), py::arg("sync") = true)
    .def("gait_start", &Soccerv3Native::gaitStart)
    .def("gait_stop", &Soccerv3Native::gaitStop)
    .def("gait_step", &Soccerv3Native::gaitStep)
    .def("set_x_amplitude", &Soccerv3Native::setXAmplitude)
    .def("set_y_amplitude", &Soccerv3Native::setYAmplitude)
    .def("set_a_amplitude", &Soccerv3Native::setAAmplitude)
    .def("set_balance_enable", &Soccerv3Native::setBalanceEnable);

  // Motor index constants matching Soccer.cpp ordering.
  m.attr("NMOTORS") = NMOTORS;
  m.attr("MOTOR_NECK") = 18;
  m.attr("MOTOR_HEAD") = 19;
}
