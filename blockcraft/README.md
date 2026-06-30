# BlockCraft
### 🚀 Getting Started

#### Running a Local Client

To get the client up and running, follow these steps. A fresh install is recommended to avoid any "Module not found" errors, as the project relies on specific versions of the `three.js` library.

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/ChiefElite/blockcraft-public.git](https://github.com/ChiefElite/blockcraft-public.git)
    ```

2.  **Navigate to the client folder:**
    ```bash
    cd blockcraft-public/client
    ```

3.  **Run the client:**
    ```bash
    npm install three
    npm install
    npm start
    ```
    The client will be available at `http://localhost:3001` by default.

---

### 💻 Running a Local Server

If you want to run the server locally, here are the steps:

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/ChiefElite/blockcraft-public.git
    ```

2.  **Navigate to the server folder:**
    ```bash
    cd blockcraft/server
    ```

3.  **Install the server dependencies:**
    ```bash
    npm install
    ```

4.  **Set up the environment file:**
    ```bash
    cp .env.example .env
    ```

5.  **Start the server:**
    ```bash
    npm start
    ```
    The server will be available for direct connections at `http://localhost:3002`.
